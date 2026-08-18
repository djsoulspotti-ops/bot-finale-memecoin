"""
risk_manager.py — Gestione del capitale e delle posizioni aperte.

Con ~100 EUR di capitale la sopravvivenza viene prima del profitto:
  - max 15% del capitale per trade, max 4 posizioni contemporanee
  - stop loss -22%, ladder di take profit a x1.8 / x3 / x6 / x15
  - trailing stop dinamico per fascia di multiplo, attivo solo sopra x1.25
  - uscita a tempo: 25 min senza movimento, 6 ore comunque
  - circuit breaker: -20% in un giorno → pausa 24 ore

DUE CORREZIONI IMPORTANTI RISPETTO ALLA VERSIONE PRECEDENTE
-----------------------------------------------------------
1. TIER DEL LADDER PERSI PER SEMPRE. `decisione_uscita` faceva
   `pos.tiers_eseguiti.append(tier)` PRIMA che la vendita venisse tentata. Se
   lo swap falliva — cosa normale su memecoin, basta un preflight fallito — il
   tier restava marcato come eseguito e al ciclo successivo non scattava più:
   quel take-profit era perso definitivamente anche col prezzo ancora sopra il
   livello. Ora `decisione_uscita` restituisce i tier CANDIDATI e vengono
   marcati solo in `registra_uscita()`, dopo la conferma della vendita.

2. MOTIVO DELL'USCITA FALSATO. Il chiamante reinferiva il motivo con
   `"ladder" if pos.tiers_eseguiti else "trailing_o_time"`, quindi un trailing
   stop scattato DOPO un tier del ladder veniva registrato come "ladder". Ma
   `agent.py` decide gli aggiustamenti automatici proprio su quelle quote, e
   `analizza_segnali.py` ci basa l'analisi del win rate: entrambi ragionavano
   su dati distorti verso "il sistema funziona". Ora il motivo reale viaggia
   insieme alla decisione.
"""

import json
import logging
import time
from dataclasses import dataclass, field, asdict

from config import CONFIG

log = logging.getLogger("risk")


@dataclass
class Posizione:
    mint: str
    symbol: str
    prezzo_entrata: float          # USD
    quantita_raw: int              # unità minime residue
    quantita_iniziale_raw: int     # unità minime all'apertura
    sol_investiti: float
    aperta_ts: float = field(default_factory=time.time)
    prezzo_massimo: float = 0.0
    tiers_eseguiti: list = field(default_factory=list)
    decimals: int = 6
    # Segnali all'ingresso, per l'analisi post-hoc del win rate per fascia
    score_locale: float | None = None
    sentiment_score: float | None = None
    momentum_score: float | None = None
    score_composito: float | None = None
    regime_entrata: str | None = None

    def aggiorna_massimo(self, prezzo: float):
        self.prezzo_massimo = max(self.prezzo_massimo, prezzo, self.prezzo_entrata)

    def pnl_pct(self, prezzo_corrente: float) -> float:
        if self.prezzo_entrata <= 0:
            return 0.0
        return (prezzo_corrente - self.prezzo_entrata) / self.prezzo_entrata

    def multiplo(self, prezzo_corrente: float) -> float:
        return prezzo_corrente / self.prezzo_entrata if self.prezzo_entrata > 0 else 0.0

    @property
    def minuti_aperta(self) -> float:
        return (time.time() - self.aperta_ts) / 60


@dataclass
class DecisioneUscita:
    """Esito di una valutazione di uscita. `motivo` è il motivo REALE, non
    reinferito dal chiamante, e `tiers_candidati` va marcato solo dopo che la
    vendita è stata confermata."""
    azione: str                      # "HOLD" | "SELL_PARTIAL" | "SELL_ALL"
    frazione: float = 0.0            # frazione del RESIDUO da vendere
    motivo: str = "hold"
    urgente: bool = False            # stop loss / volatility: non attendere il calm
    tiers_candidati: list = field(default_factory=list)


class RiskManager:
    def __init__(self):
        self.posizioni: dict[str, Posizione] = {}
        self.capitale_eur = CONFIG.risk.capitale_iniziale_eur
        # Baseline REALE per circuit breaker e stop di sicurezza: parte dal
        # default di config.py ma viene sovrascritta col saldo vero del wallet
        # al primo avvio live. Le soglie percentuali devono ancorarsi ai soldi
        # che ci sono davvero.
        self.capitale_iniziale_eur_effettivo = CONFIG.risk.capitale_iniziale_eur
        self.pnl_giornaliero_eur = 0.0
        self.giorno_corrente = time.strftime("%Y-%m-%d")
        self.in_pausa_fino: float = 0.0

    @property
    def r(self):
        # Letto ad ogni accesso: il supervisore modifica le soglie a runtime.
        return CONFIG.risk

    # ---------- CONTROLLI PRE-TRADE ----------

    def puo_aprire(self) -> tuple[bool, str]:
        self._reset_giornaliero()
        if self.in_pausa_fino == float("inf"):
            return False, "STOP DI SICUREZZA attivo (valore reale sotto soglia) — richiede 'control.py start'"
        if time.time() < self.in_pausa_fino:
            ore = (self.in_pausa_fino - time.time()) / 3600
            return False, f"circuit breaker attivo per altre {ore:.1f}h"
        if len(self.posizioni) >= self.r.max_posizioni_aperte:
            return False, f"già {len(self.posizioni)} posizioni aperte (max)"
        return True, "ok"

    def imposta_capitale_iniziale_reale(self, valore_eur: float):
        """Chiamato UNA SOLA VOLTA, al primissimo avvio live senza stato
        precedente, col saldo reale del wallet convertito in EUR."""
        self.capitale_iniziale_eur_effettivo = valore_eur
        self.capitale_eur = valore_eur
        self._salva_stato()
        log.info("💰 Capitale iniziale ancorato al saldo reale del wallet: %.2f€", valore_eur)

    def forza_pausa_sicurezza(self) -> bool:
        """Pausa INDEFINITA forzata dal controllo sul valore reale del wallet.
        A differenza del circuit breaker giornaliero (si riapre da solo dopo
        24h) richiede un reset manuale esplicito. Ritorna True solo al primo
        trigger, per non ripetere l'allarme ad ogni ciclo."""
        era_attiva = self.in_pausa_fino == float("inf")
        self.in_pausa_fino = float("inf")
        return not era_attiva

    def size_posizione_eur(self, score_composito: float | None = None) -> float:
        """Kelly semplificato: la size scala con la qualità del segnale."""
        base = self.r.max_posizione_pct
        if self.r.kelly_sizing and score_composito is not None:
            frac = max(self.r.min_posizione_pct, base * (score_composito / 100.0))
        else:
            frac = base
        return round(self.capitale_eur * frac, 2)

    def _trailing_pct(self, multiplo: float) -> float:
        for soglia, pct in self.r.trailing_bands:
            if multiplo < soglia:
                return pct
        return self.r.trailing_bands[-1][1]

    # ---------- GESTIONE POSIZIONI ----------

    def apri(self, pos: Posizione):
        self.posizioni[pos.mint] = pos
        log.info("📈 Aperta %s @ $%.10f (%.4f SOL, %d unità)",
                 pos.symbol, pos.prezzo_entrata, pos.sol_investiti, pos.quantita_raw)
        self._salva_stato()

    def decisione_uscita(self, mint: str, prezzo_corrente: float,
                         price_change_5m: float | None = None) -> DecisioneUscita:
        pos = self.posizioni.get(mint)
        if not pos or prezzo_corrente <= 0:
            return DecisioneUscita("HOLD")

        pos.aggiorna_massimo(prezzo_corrente)
        pnl = pos.pnl_pct(prezzo_corrente)
        multiplo = pos.multiplo(prezzo_corrente)

        # 1. STOP LOSS — urgente, vendita secca di tutto il residuo
        if pnl <= self.r.stop_loss_pct:
            log.warning("🛑 STOP LOSS %s: %.1f%%", pos.symbol, pnl * 100)
            return DecisioneUscita("SELL_ALL", 1.0, "stop_loss", urgente=True)

        # 2. VOLATILITY STOP — solo crollo improvviso mentre siamo in profitto.
        #
        # Prima la condizione era `abs(price_change_5m) >= soglia`, quindi
        # trattava +91% e -91% allo stesso modo. Risultato osservato in paper
        # trading: il bot comprava DELONTE perché il momentum era 91 e tre
        # secondi dopo lo vendeva perché la volatilità era 91 — la stessa
        # misura guidava l'ingresso e l'uscita in direzioni opposte, con la
        # sola certezza di pagare due volte le fee.
        #
        # Una salita violenta non è un motivo per uscire: per quella c'è il
        # trailing stop, che protegge il guadagno lasciando correre la
        # posizione. Il volatility stop serve al caso opposto, il crollo.
        if (pnl > self.r.volatility_stop_min_pnl and price_change_5m is not None
                and price_change_5m <= -self.r.volatility_stop_change5m):
            log.warning("🌪️ VOLATILITY STOP %s: crollo 5m %.0f%% con pnl %+.0f%%",
                        pos.symbol, price_change_5m, pnl * 100)
            return DecisioneUscita("SELL_ALL", 1.0, "volatility_stop", urgente=True)

        # Periodo di grazia: nei primi secondi dopo l'ingresso i dati di
        # mercato riflettono ancora il movimento su cui siamo entrati, e
        # trailing o uscite a tempo scatterebbero sul rumore dell'ingresso
        # stesso. Lo STOP LOSS resta escluso da questa grazia: quello deve
        # poter scattare sempre e subito, ed è già stato valutato sopra.
        if (time.time() - pos.aperta_ts) < self.r.grazia_ingresso_sec:
            return DecisioneUscita("HOLD")

        # 3. LADDER DI TAKE PROFIT — prima del trailing: se il prezzo ha
        #    superato un tier, la vendita programmata ha priorità sull'uscita
        #    difensiva. Le frazioni sono riferite alla posizione ORIGINALE.
        frazione_orig = 0.0
        tiers_candidati = []
        for tier_mult, tier_fraz in self.r.tp_ladder:
            if multiplo >= tier_mult and tier_mult not in pos.tiers_eseguiti:
                tiers_candidati.append(tier_mult)
                frazione_orig += tier_fraz
        if frazione_orig > 0:
            qty_da_vendere = pos.quantita_iniziale_raw * frazione_orig
            frazione_residuo = min(1.0, qty_da_vendere / max(pos.quantita_raw, 1))
            log.info("🎯 LADDER %s a %.2fx → vendo %.0f%% dell'originale (tier %s)",
                     pos.symbol, multiplo, frazione_orig * 100, tiers_candidati)
            azione = "SELL_ALL" if frazione_residuo >= 0.999 else "SELL_PARTIAL"
            return DecisioneUscita(azione, frazione_residuo, "ladder",
                                   tiers_candidati=tiers_candidati)

        # 4. TRAILING STOP dinamico. Attivo solo sopra
        #    `trailing_attivo_da_multiplo`: sotto quel livello comanda lo stop
        #    loss, altrimenti il rumore dei primi minuti farebbe uscire da
        #    ogni posizione appena aperta.
        if pos.prezzo_massimo / max(pos.prezzo_entrata, 1e-18) >= self.r.trailing_attivo_da_multiplo:
            trail = self._trailing_pct(pos.prezzo_massimo / pos.prezzo_entrata)
            drawdown = (prezzo_corrente - pos.prezzo_massimo) / pos.prezzo_massimo
            if drawdown <= -trail:
                log.info("📉 TRAILING STOP %s (-%.0f%% dal max, multiplo %.2fx, trail %.0f%%)",
                         pos.symbol, -drawdown * 100, multiplo, trail * 100)
                return DecisioneUscita("SELL_ALL", 1.0, "trailing_stop")

        # 5. USCITE A TEMPO
        if (pos.minuti_aperta >= self.r.momentum_check_minuti
                and pnl < self.r.momentum_check_min_pnl and not pos.tiers_eseguiti):
            log.info("⏱️ NO MOMENTUM %s dopo %.0f min (pnl %+.1f%%)",
                     pos.symbol, pos.minuti_aperta, pnl * 100)
            return DecisioneUscita("SELL_ALL", 1.0, "no_momentum")
        if pos.minuti_aperta >= self.r.max_holding_ore * 60:
            log.info("⏱️ MAX HOLDING %s dopo %.1fh (multiplo %.2fx)",
                     pos.symbol, pos.minuti_aperta / 60, multiplo)
            return DecisioneUscita("SELL_ALL", 1.0, "max_holding")

        return DecisioneUscita("HOLD")

    def registra_uscita(self, mint: str, pnl_eur: float, frazione: float,
                        motivo: str, tiers_eseguiti: list | None = None) -> bool:
        """Aggiorna la contabilità DOPO una vendita confermata.

        `frazione` è la frazione del residuo realmente venduta, e
        `tiers_eseguiti` viene marcato solo qui: un tier del ladder la cui
        vendita è fallita resta disponibile per il tentativo successivo.

        Ritorna True se questa chiusura ha fatto scattare il circuit breaker.
        """
        pos = self.posizioni.get(mint)
        if not pos:
            return False

        if tiers_eseguiti:
            for t in tiers_eseguiti:
                if t not in pos.tiers_eseguiti:
                    pos.tiers_eseguiti.append(t)

        self.capitale_eur += pnl_eur
        self.pnl_giornaliero_eur += pnl_eur

        with open("trade_chiusi.jsonl", "a") as f:
            f.write(json.dumps({
                "ts": time.time(), "symbol": pos.symbol, "mint": mint,
                "pnl_eur": round(pnl_eur, 4), "frazione": round(frazione, 4),
                "motivo": motivo, "minuti": round(pos.minuti_aperta, 1),
                "score_locale": pos.score_locale,
                "sentiment_score": pos.sentiment_score,
                "momentum_score": pos.momentum_score,
                "score_composito": pos.score_composito,
                "regime_entrata": pos.regime_entrata,
                "tiers_eseguiti": list(pos.tiers_eseguiti),
            }) + "\n")

        if frazione >= 0.999:
            del self.posizioni[mint]
        else:
            pos.quantita_raw = max(0, int(pos.quantita_raw * (1 - frazione)))
            if pos.quantita_raw == 0:
                del self.posizioni[mint]

        log.info("💰 %s: PnL %+.2f EUR (%s) | capitale=%.2f EUR",
                 pos.symbol, pnl_eur, motivo, self.capitale_eur)

        soglia = -self.capitale_iniziale_eur_effettivo * self.r.max_perdita_giornaliera_pct
        breaker = False
        if self.pnl_giornaliero_eur <= soglia and self.in_pausa_fino != float("inf"):
            self.in_pausa_fino = time.time() + 24 * 3600
            breaker = True
            log.warning("🚨 CIRCUIT BREAKER: perdita giornaliera %.2f EUR → pausa 24h",
                        self.pnl_giornaliero_eur)
        self._salva_stato()
        return breaker

    # ---------- PERSISTENZA ----------

    def _reset_giornaliero(self):
        oggi = time.strftime("%Y-%m-%d")
        if oggi != self.giorno_corrente:
            self.giorno_corrente = oggi
            self.pnl_giornaliero_eur = 0.0

    def _salva_stato(self):
        stato = {
            "capitale_eur": self.capitale_eur,
            "capitale_iniziale_eur_effettivo": self.capitale_iniziale_eur_effettivo,
            "pnl_giornaliero_eur": self.pnl_giornaliero_eur,
            "giorno_corrente": self.giorno_corrente,
            "in_pausa_fino": self.in_pausa_fino if self.in_pausa_fino != float("inf") else "inf",
            "posizioni": {m: asdict(p) for m, p in self.posizioni.items()},
        }
        try:
            with open("stato_bot.json", "w") as f:
                json.dump(stato, f, indent=2)
        except OSError as e:
            log.error("Impossibile salvare stato_bot.json: %s", e)

    def carica_stato(self) -> bool:
        """Ritorna True se ha trovato e ripristinato uno stato precedente: il
        chiamante lo usa per NON rilevare il capitale dal wallet sopra una
        contabilità già esistente."""
        try:
            with open("stato_bot.json") as f:
                stato = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            log.info("Nessuno stato precedente: partenza pulita")
            return False

        self.capitale_eur = stato.get("capitale_eur", self.capitale_eur)
        self.capitale_iniziale_eur_effettivo = stato.get(
            "capitale_iniziale_eur_effettivo", self.capitale_iniziale_eur_effettivo)
        self.pnl_giornaliero_eur = stato.get("pnl_giornaliero_eur", 0.0)
        self.giorno_corrente = stato.get("giorno_corrente", self.giorno_corrente)

        pausa = stato.get("in_pausa_fino", 0.0)
        # Uno stop di sicurezza sopravvive al riavvio: era il suo scopo.
        self.in_pausa_fino = float("inf") if pausa == "inf" else float(pausa or 0.0)

        campi_validi = set(Posizione.__dataclass_fields__)
        for m, p in (stato.get("posizioni") or {}).items():
            try:
                self.posizioni[m] = Posizione(**{k: v for k, v in p.items() if k in campi_validi})
            except TypeError as e:
                log.error("Posizione %s non ripristinabile (%s): la salto", m[:8], e)

        log.info("Stato ripristinato: capitale=%.2f EUR, %d posizioni%s",
                 self.capitale_eur, len(self.posizioni),
                 " [STOP DI SICUREZZA ATTIVO]" if self.in_pausa_fino == float("inf") else "")
        return True
