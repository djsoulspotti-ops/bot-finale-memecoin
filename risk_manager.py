"""
risk_manager.py — Gestione del capitale e delle posizioni aperte.

Con 100 EUR di capitale la sopravvivenza viene prima del profitto:
  - max 15% del capitale per trade (~15 EUR)
  - max 3 posizioni contemporanee
  - stop loss -30%, take profit scalato (+50% vendi metà, +150% vendi tutto)
  - trailing stop 25% dal massimo raggiunto
  - uscita a tempo dopo 120 min
  - circuit breaker: -20% in un giorno → pausa 24 ore
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
    prezzo_entrata: float          # in USD
    quantita_raw: int              # unità minime del token (residue)
    quantita_iniziale_raw: int     # unità minime al momento dell'apertura
    sol_investiti: float
    aperta_ts: float = field(default_factory=time.time)
    prezzo_massimo: float = 0.0
    tiers_eseguiti: list = field(default_factory=list)  # multipli già scattati, es. [5.0]
    # Segnali al momento dell'ingresso (per l'analisi post-hoc del win rate per fascia)
    claude_score: float | None = None
    sentiment_score: float | None = None
    momentum_score: float | None = None
    score_composito: float | None = None
    regime_entrata: str | None = None

    def aggiorna_massimo(self, prezzo: float):
        self.prezzo_massimo = max(self.prezzo_massimo, prezzo, self.prezzo_entrata)

    def pnl_pct(self, prezzo_corrente: float) -> float:
        return (prezzo_corrente - self.prezzo_entrata) / self.prezzo_entrata

    @property
    def minuti_aperta(self) -> float:
        return (time.time() - self.aperta_ts) / 60


class RiskManager:
    def __init__(self):
        self.r = CONFIG.risk
        self.posizioni: dict[str, Posizione] = {}
        self.capitale_eur = self.r.capitale_iniziale_eur
        self.pnl_giornaliero_eur = 0.0
        self.giorno_corrente = time.strftime("%Y-%m-%d")
        self.in_pausa_fino: float = 0.0

    # ---------- CONTROLLI PRE-TRADE ----------

    def puo_aprire(self) -> tuple[bool, str]:
        self._reset_giornaliero()
        if self.in_pausa_fino == float("inf"):
            return False, "STOP DI SICUREZZA attivo (saldo reale sotto soglia) — richiede 'python control.py start'"
        if time.time() < self.in_pausa_fino:
            ore = (self.in_pausa_fino - time.time()) / 3600
            return False, f"circuit breaker attivo per altre {ore:.1f}h"
        if len(self.posizioni) >= self.r.max_posizioni_aperte:
            return False, f"già {len(self.posizioni)} posizioni aperte (max)"
        return True, "ok"

    def forza_pausa_sicurezza(self) -> bool:
        """Pausa INDEFINITA forzata da un controllo esterno (saldo wallet reale
        sotto la soglia di sicurezza). A differenza del circuit breaker
        giornaliero (si riapre da solo dopo 24h), questa richiede un reset
        manuale esplicito dell'utente. Ritorna True solo al primo trigger
        (per non spammare il log ad ogni ciclo)."""
        era_gia_attiva = self.in_pausa_fino == float("inf")
        self.in_pausa_fino = float("inf")
        return not era_gia_attiva

    def size_posizione_eur(self, score_composito: float | None = None) -> float:
        """
        Quanto investire nel prossimo trade.
        Kelly semplificato: la size scala con la qualità del segnale.
        Score 90 → ~13.5% | score 60 → 9% | pavimento al 5%.
        """
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
        log.info("📈 Aperta posizione %s @ $%.8f (%.2f SOL)", pos.symbol, pos.prezzo_entrata, pos.sol_investiti)
        self._salva_stato()

    def decisione_uscita(self, mint: str, prezzo_corrente: float,
                         price_change_5m: float | None = None) -> tuple[str, float]:
        """
        Ritorna (azione, frazione_da_vendere):
          ("HOLD", 0) | ("SELL_PARTIAL", 0.5) | ("SELL_ALL", 1.0)
        """
        pos = self.posizioni.get(mint)
        if not pos:
            return "HOLD", 0.0

        pos.aggiorna_massimo(prezzo_corrente)
        pnl = pos.pnl_pct(prezzo_corrente)
        multiplo = prezzo_corrente / pos.prezzo_entrata

        # Stop loss (frazione 1.0 = tutto il residuo)
        if pnl <= self.r.stop_loss_pct:
            log.warning("🛑 STOP LOSS %s: %.1f%%", pos.symbol, pnl * 100)
            return "SELL_ALL", 1.0

        # Volatility stop: in profitto e mercato in panico (|var 5m| enorme) → esci
        if pnl > 0 and price_change_5m is not None and abs(price_change_5m) >= self.r.volatility_stop_change5m:
            log.warning("🌪️ VOLATILITY STOP %s: var 5m %.0f%% con pnl +%.0f%% → esco", pos.symbol, price_change_5m, pnl * 100)
            return "SELL_ALL", 1.0

        # Trailing stop DINAMICO per fascia di multiplo (solo se siamo stati in profitto)
        if pos.prezzo_massimo > pos.prezzo_entrata:
            trail = self._trailing_pct(pos.prezzo_massimo / pos.prezzo_entrata)
            drawdown = (prezzo_corrente - pos.prezzo_massimo) / pos.prezzo_massimo
            if drawdown <= -trail:
                log.info("📉 TRAILING STOP %s (-%.0f%% dal max, multiplo %.1fx, trail %.0f%%)",
                         pos.symbol, -drawdown * 100, multiplo, trail * 100)
                return "SELL_ALL", 1.0

        # LADDER x5/x10/x50 — frazioni riferite alla POSIZIONE ORIGINALE.
        # Se il prezzo salta più tier in un colpo (gap), scattano insieme.
        frazione_orig = 0.0
        for tier_mult, tier_fraz in self.r.tp_ladder:
            if multiplo >= tier_mult and tier_mult not in pos.tiers_eseguiti:
                pos.tiers_eseguiti.append(tier_mult)
                frazione_orig += tier_fraz
        if frazione_orig > 0:
            # Converti la frazione dell'originale in frazione del RESIDUO
            qty_da_vendere = pos.quantita_iniziale_raw * frazione_orig
            frazione_residuo = min(1.0, qty_da_vendere / max(pos.quantita_raw, 1))
            log.info("🎯 LADDER %s a %.1fx → vendo %.0f%% dell'originale (tiers: %s)",
                     pos.symbol, multiplo, frazione_orig * 100, pos.tiers_eseguiti)
            return ("SELL_ALL", 1.0) if frazione_residuo >= 0.999 else ("SELL_PARTIAL", frazione_residuo)

        # Time exit a due stadi
        if pos.minuti_aperta >= self.r.momentum_check_minuti and pnl < self.r.momentum_check_min_pnl and not pos.tiers_eseguiti:
            log.info("⏱️ NO MOMENTUM %s dopo %.0f min (pnl %.1f%%) → esco", pos.symbol, pos.minuti_aperta, pnl * 100)
            return "SELL_ALL", 1.0
        if pos.minuti_aperta >= self.r.max_holding_ore * 60:
            log.info("⏱️ MAX HOLDING %s dopo %.0fh → chiudo (multiplo %.1fx)", pos.symbol, pos.minuti_aperta / 60, multiplo)
            return "SELL_ALL", 1.0

        return "HOLD", 0.0

    def chiudi(self, mint: str, pnl_eur: float, frazione: float = 1.0, motivo: str = "?") -> bool:
        """Ritorna True se questa chiusura ha fatto scattare il circuit breaker
        giornaliero (utile al chiamante per mandare un alert immediato)."""
        pos = self.posizioni.get(mint)
        if not pos:
            return False
        self.capitale_eur += pnl_eur
        self.pnl_giornaliero_eur += pnl_eur
        with open("trade_chiusi.jsonl", "a") as f:
            f.write(json.dumps({
                "ts": time.time(), "symbol": pos.symbol, "mint": mint,
                "pnl_eur": round(pnl_eur, 2), "frazione": frazione,
                "motivo": motivo, "minuti": round(pos.minuti_aperta, 1),
                # Segnali al momento dell'ingresso: servono per capire, con dati
                # veri, quale fascia di score correla davvero con i trade vincenti.
                "claude_score": pos.claude_score,
                "sentiment_score": pos.sentiment_score,
                "momentum_score": pos.momentum_score,
                "score_composito": pos.score_composito,
                "regime_entrata": pos.regime_entrata,
            }) + "\n")
        if frazione >= 1.0:
            del self.posizioni[mint]
        else:
            pos.quantita_raw = int(pos.quantita_raw * (1 - frazione))
        log.info("💰 %s: PnL %+.2f EUR | capitale=%.2f EUR", pos.symbol, pnl_eur, self.capitale_eur)

        # Circuit breaker
        soglia = -self.r.capitale_iniziale_eur * self.r.max_perdita_giornaliera_pct
        breaker_scattato = False
        if self.pnl_giornaliero_eur <= soglia and self.in_pausa_fino != float("inf"):
            self.in_pausa_fino = time.time() + 24 * 3600
            breaker_scattato = True
            log.warning("🚨 CIRCUIT BREAKER: perdita giornaliera %.2f EUR → pausa 24h", self.pnl_giornaliero_eur)
        self._salva_stato()
        return breaker_scattato

    # ---------- PERSISTENZA ----------

    def _reset_giornaliero(self):
        oggi = time.strftime("%Y-%m-%d")
        if oggi != self.giorno_corrente:
            self.giorno_corrente = oggi
            self.pnl_giornaliero_eur = 0.0

    def _salva_stato(self):
        stato = {
            "capitale_eur": self.capitale_eur,
            "pnl_giornaliero_eur": self.pnl_giornaliero_eur,
            "posizioni": {m: asdict(p) for m, p in self.posizioni.items()},
        }
        with open("stato_bot.json", "w") as f:
            json.dump(stato, f, indent=2)

    def carica_stato(self):
        try:
            with open("stato_bot.json") as f:
                stato = json.load(f)
            self.capitale_eur = stato.get("capitale_eur", self.capitale_eur)
            for m, p in stato.get("posizioni", {}).items():
                self.posizioni[m] = Posizione(**p)
            log.info("Stato ripristinato: capitale=%.2f EUR, %d posizioni", self.capitale_eur, len(self.posizioni))
        except FileNotFoundError:
            log.info("Nessuno stato precedente: partenza pulita")
