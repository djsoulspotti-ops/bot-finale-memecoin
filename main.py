"""
main.py — Orchestratore del bot.

ARCHITETTURA: TRE LOOP INDIPENDENTI
-----------------------------------
La versione precedente aveva UN solo loop da 20 secondi che faceva discovery
e monitoraggio in sequenza, e dentro cui una vendita a tranche poteva
bloccare tutto per 45 minuti (3 tranche × 15 minuti di attesa del market
calm). In quei 45 minuti: nessuna scansione, nessun controllo del saldo, e
soprattutto nessuno stop loss controllato sulle ALTRE posizioni aperte.

Ora girano tre task asyncio separati che non si aspettano tra loro:

  loop_discovery  ~4 s   scan → filtri → scoring → cluster → acquisto
  loop_monitor    ~1.5 s prezzi in batch → SL/TP/trailing su ogni posizione
  loop_sorveglianza ~20 s  controllo comando manuale + floor di sicurezza

Le vendite partono come task separati con un lock per mint: il loop di
monitoraggio registra la decisione e va avanti, non resta appeso
all'esecuzione. Il lock evita che due cicli consecutivi lancino due vendite
sovrapposte sulla stessa posizione.

Pipeline d'ingresso:
  1. SCAN      → scanner.py, tre feed Jupiter uniti (~105 candidati/ciclo)
  2. FILTER    → filters.py, percorso veloce senza I/O
  3. MOMENTUM  → timing.py
  4. SENTIMENT → sentiment.py, dai campi social già nel feed
  5. SCORE     → local_analyzer.py, formula locale su 5 dimensioni
  6. CLUSTER   → 45 s di raccolta, poi si comprano i migliori N
  7. DEEP CHECK→ filters.deep_check, una sola chiamata RugCheck sul prescelto
  8. EXECUTE   → executor.py via Jupiter

Controllo manuale (control.py / Telegram): run | pausa | stop
  run:   ingressi + monitoraggio
  pausa: nessun nuovo ingresso, monitoraggio e protezione ATTIVI
  stop:  tutto fermo, posizioni NON più protette

Avvio: python main.py — DEFAULT PAPER. Per operare con soldi veri serve un
BOT_MODE=live esplicito nel .env.
"""

import asyncio
import json
import logging
import sys
import time

import aiohttp

from agent import AgentSupervisor
from config import CONFIG
from executor import JupiterExecutor
from filters import TokenFilter
from local_analyzer import LocalAnalyzer
from market_conditions import MarketConditions
from market_sentiment import MarketSentiment
from metrics import IMBUTO
from risk_manager import Posizione, RiskManager
from scanner import PoolScanner
from sentiment import SentimentAnalyzer
from telegram_bot import TelegramNotifier
from timing import momentum_score, score_composito

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-9s | %(levelname)-7s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("bot.log")],
)
log = logging.getLogger("main")

SOL_MINT = "So11111111111111111111111111111111111111112"


def leggi_comando_controllo() -> str:
    """Legge control.json (scritto da control.py o dal bot Telegram)."""
    try:
        with open(CONFIG.file_controllo) as f:
            return json.load(f).get("comando", "run")
    except (FileNotFoundError, json.JSONDecodeError):
        return "run"


class MemecoinBot:
    def __init__(self, session: aiohttp.ClientSession):
        self.scanner = PoolScanner(session)
        self.filtro = TokenFilter(session)
        self.analizzatore = LocalAnalyzer()
        self.sentiment = SentimentAnalyzer(session)
        self.market = MarketConditions(session)
        self.mkt_sentiment = MarketSentiment(session)
        self.executor = JupiterExecutor(session)
        self.risk = RiskManager()
        self._stato_precedente = self.risk.carica_stato()
        self.telegram = TelegramNotifier(session)
        self.agente = AgentSupervisor(session, self.risk, self.telegram)
        self.session = session

        self.prezzo_sol_eur = 0.0
        self._prezzo_sol_ts = 0.0
        self.comando = "run"

        # Cluster: (candidato, score_composito, score_locale, sentiment, momentum)
        self.cluster: list[tuple] = []
        self.cluster_apertura_ts = 0.0
        self._sem = asyncio.Semaphore(CONFIG.max_candidati_paralleli)
        # Un lock per mint: impedisce vendite sovrapposte sulla stessa posizione
        self._lock_uscita: dict[str, asyncio.Lock] = {}
        self._uscite_in_corso: set[str] = set()
        self._regime_cache: dict = {"regime": "NORMALE"}
        self._mod_cache: dict = MarketSentiment.modulatori({"regime": "NORMALE"})

    # ================= VALUTAZIONE CANDIDATI =================

    async def _valuta_candidato(self, c, mod: dict) -> tuple | None:
        """Percorso di valutazione di un singolo candidato. Nessuna chiamata
        di rete: filtri, momentum, sentiment e score girano tutti su dati che
        lo scanner ha già raccolto in un'unica richiesta per feed."""
        async with self._sem:
            IMBUTO.valutato()

            fr = self.filtro.valuta_sync(c)
            if not fr.passed:
                IMBUTO.scarto(fr.stadio, fr.motivi_scarto[0])
                if fr.definitivo:
                    self.scanner.segna_definitivo(c.mint, fr.motivi_scarto[0])
                else:
                    self.scanner.segna_ricontrolla(c.mint)
                return None

            mom = momentum_score(c)
            soglia_mom = CONFIG.filters.min_momentum_score + mod["min_momentum_extra"]
            if mom < soglia_mom:
                IMBUTO.scarto("momentum", f"momentum sotto soglia (regime {self._regime_cache.get('regime')})")
                self.scanner.segna_ricontrolla(c.mint)
                return None

            sent = await self.sentiment.analizza(c)
            if sent["sentiment_score"] < CONFIG.filters.min_sentiment_score:
                IMBUTO.scarto("sentiment", "presenza social sotto soglia")
                # La presenza social non cambia col tempo: scarto definitivo.
                self.scanner.segna_definitivo(c.mint, "sentiment insufficiente")
                return None

            analisi = {"score": 75, "decisione": "COMPRA", "motivazione": "analisi locale disattivata"}
            if CONFIG.usa_analisi_locale:
                analisi = await self.analizzatore.analizza(c, fr)
                if analisi["decisione"] != "COMPRA":
                    IMBUTO.scarto("score_locale", "score locale sotto soglia")
                    self.scanner.segna_ricontrolla(c.mint)
                    return None

            comp = score_composito(analisi["score"], sent["sentiment_score"], mom)
            IMBUTO.promosso()
            # Mettilo in cooldown SUBITO. Senza questo, un candidato promosso
            # resta valutabile e viene ripescato dal feed ad ogni ciclo di
            # discovery finché la finestra del cluster non si chiude: con
            # cluster_buffer_sec=45 e scan_interval=3 lo stesso token entrava
            # nel cluster una dozzina di volte, il ranking confrontava copie
            # di sé stesso e `cluster_top_n` poteva selezionare N volte lo
            # stesso mint. Osservato: 12 copie di un solo token in un cluster
            # che ne dichiarava 12 "candidati".
            self.scanner.segna_ricontrolla(c.mint)
            log.info("🧺 %s promosso | composito %.0f (locale %d · sentiment %d · momentum %.0f) "
                     "| mcap $%s · liq $%s · %.0f min | %s",
                     c.symbol, comp, analisi["score"], sent["sentiment_score"], mom,
                     f"{c.market_cap_usd:,.0f}", f"{c.liquidity_usd:,.0f}", c.eta_minuti,
                     analisi["motivazione"])
            return (c, comp, analisi["score"], sent["sentiment_score"], mom)

    # ================= LOOP DISCOVERY =================

    async def loop_discovery(self):
        while True:
            try:
                if self.comando != "run":
                    await asyncio.sleep(CONFIG.scan_interval_sec)
                    continue

                ok, motivo = self.risk.puo_aprire()
                if not ok:
                    log.debug("Ingresso bloccato: %s", motivo)
                    await asyncio.sleep(CONFIG.scan_interval_sec)
                    continue

                await self._aggiorna_regime()
                candidati = await self.scanner.scansiona_nuovi_pool()
                if candidati:
                    risultati = await asyncio.gather(
                        *[self._valuta_candidato(c, self._mod_cache) for c in candidati],
                        return_exceptions=True,
                    )
                    nuovi = [r for r in risultati if isinstance(r, tuple)]
                    for r in risultati:
                        if isinstance(r, Exception):
                            log.error("Errore valutando un candidato: %s", r)
                    if nuovi:
                        if not self.cluster:
                            self.cluster_apertura_ts = time.time()
                        self.cluster.extend(nuovi)

                await self._forse_chiudi_cluster()
                IMBUTO.forse_logga()
            except Exception as e:
                log.exception("Errore nel loop discovery: %s", e)
            await asyncio.sleep(CONFIG.scan_interval_sec)

    async def _forse_chiudi_cluster(self):
        """Alla scadenza della finestra compra i migliori N del lotto."""
        if not self.cluster:
            return
        if time.time() - self.cluster_apertura_ts < CONFIG.risk.cluster_buffer_sec:
            return

        # Deduplica per mint tenendo lo score migliore: seconda linea di
        # difesa oltre al cooldown, perché comprare due volte lo stesso token
        # nello stesso cluster raddoppierebbe l'esposizione su un singolo
        # nome aggirando max_posizione_pct.
        per_mint: dict[str, tuple] = {}
        for voce in self.cluster:
            mint = voce[0].mint
            if mint not in per_mint or voce[1] > per_mint[mint][1]:
                per_mint[mint] = voce
        self.cluster = sorted(per_mint.values(), key=lambda x: x[1], reverse=True)

        top_n = max(1, CONFIG.risk.cluster_top_n + self._mod_cache["cluster_top_n_delta"])
        migliori, scartati = self.cluster[:top_n], self.cluster[top_n:]
        self.cluster = []

        log.info("🏁 Cluster chiuso: %d candidati, compro i top %d (regime %s): %s",
                 len(migliori) + len(scartati), len(migliori), self._regime_cache.get("regime"),
                 [(c.symbol, s) for c, s, *_ in migliori])
        # I non selezionati non sono cattivi, hanno solo perso il confronto:
        # tornano valutabili al prossimo giro.
        for c, *_ in scartati:
            self.scanner.segna_ricontrolla(c.mint)

        for c, comp, loc, sent, mom in migliori:
            ok, _ = self.risk.puo_aprire()
            if not ok:
                self.scanner.segna_ricontrolla(c.mint)
                continue
            await self._prova_acquisto(c, comp, loc, sent, mom)

    async def _prova_acquisto(self, c, comp, loc, sent, mom):
        """Rivalidazione fresca + deep check + esecuzione."""
        # I dati del candidato hanno fino a `cluster_buffer_sec` secondi:
        # ricontrolla il prezzo ADESSO prima di impegnare capitale.
        info = await self.market.prezzi([c.mint])
        fresco = info.get(c.mint, {}).get("prezzo")
        if not fresco:
            IMBUTO.scarto("rivalidazione", "prezzo fresco non disponibile")
            self.scanner.segna_ricontrolla(c.mint)
            return

        variazione = (fresco - c.price_usd) / c.price_usd if c.price_usd > 0 else 0.0
        if variazione > 0.25:
            log.info("⏭️ %s: %+.0f%% dallo scoring, rischio tetto del pump → salto",
                     c.symbol, variazione * 100)
            IMBUTO.scarto("rivalidazione", "pumpato dopo lo scoring")
            self.scanner.segna_ricontrolla(c.mint)
            return
        if variazione < -0.20:
            log.info("⏭️ %s: %+.0f%% dallo scoring, segnale decaduto → salto",
                     c.symbol, variazione * 100)
            IMBUTO.scarto("rivalidazione", "segnale decaduto")
            self.scanner.segna_ricontrolla(c.mint)
            return

        deep = await self.filtro.deep_check(c)
        if not deep.passed:
            log.info("⛔ %s bocciato al deep check: %s", c.symbol, "; ".join(deep.motivi_scarto))
            IMBUTO.scarto("deep_check", deep.motivi_scarto[0] if deep.motivi_scarto else "deep check")
            if deep.definitivo:
                self.scanner.segna_definitivo(c.mint, "deep check")
            else:
                self.scanner.segna_ricontrolla(c.mint)
            return

        c.price_usd = fresco  # prezzo d'entrata al valore fresco (statistiche pulite)
        await self._apri_posizione(c, comp, loc, sent, mom)

    async def _apri_posizione(self, c, score_comp, score_loc, sent, mom):
        await self._aggiorna_prezzo_sol()
        if self.prezzo_sol_eur <= 0:
            log.error("Prezzo SOL non disponibile: salto l'acquisto di %s", c.symbol)
            IMBUTO.scarto("esecuzione", "prezzo SOL non disponibile")
            return

        size_eur = self.risk.size_posizione_eur(score_comp) * self._mod_cache["size_mult"]
        sol_amount = size_eur / self.prezzo_sol_eur

        res = await self.executor.compra(c.mint, sol_amount)
        IMBUTO.acquisto(res.ok)
        if not res.ok:
            log.error("Acquisto %s fallito: %s", c.symbol, res.errore)
            self.scanner.segna_ricontrolla(c.mint)
            if CONFIG.mode == "live":
                await self.telegram.invia(f"⚠️ Acquisto <b>{c.symbol}</b> fallito: {res.errore}")
            return

        qty = int(res.output_amount)
        if qty <= 0:
            log.error("Acquisto %s: quantità ricevuta nulla, non registro la posizione", c.symbol)
            return

        self.scanner.segna_definitivo(c.mint, "comprato")
        self.risk.apri(Posizione(
            mint=c.mint, symbol=c.symbol, prezzo_entrata=c.price_usd,
            quantita_raw=qty, quantita_iniziale_raw=qty, sol_investiti=sol_amount,
            decimals=c.decimals, score_locale=score_loc, sentiment_score=sent,
            momentum_score=mom, score_composito=score_comp,
            regime_entrata=self._regime_cache.get("regime"),
        ))
        nota = " (recuperata da saldo on-chain dopo conferma tardiva)" if res.recuperato_da_saldo else ""
        await self.telegram.invia(
            f"📈 Aperta posizione <b>{c.symbol}</b>{nota}\n"
            f"Prezzo: ${c.price_usd:.10f}\n"
            f"Investiti: {sol_amount:.4f} SOL (≈{size_eur:.2f}€)\n"
            f"Score composito: {score_comp:.0f} (locale {score_loc} · sentiment {sent} · momentum {mom:.0f})\n"
            f"Mcap ${c.market_cap_usd:,.0f} · Liquidità ${c.liquidity_usd:,.0f} · {c.dex}"
        )

    # ================= LOOP MONITORAGGIO =================

    async def loop_monitor(self):
        while True:
            try:
                if self.comando == "stop":
                    await asyncio.sleep(CONFIG.monitor_interval_sec)
                    continue

                mints = [m for m in self.risk.posizioni if m not in self._uscite_in_corso]
                if mints:
                    # UNA richiesta per tutte le posizioni: il costo del loop
                    # di rischio non cresce col numero di posizioni aperte.
                    prezzi = await self.market.prezzi(mints)
                    for mint, info in prezzi.items():
                        self._valuta_uscita(mint, info)
            except Exception as e:
                log.exception("Errore nel loop monitor: %s", e)
            await asyncio.sleep(CONFIG.monitor_interval_sec)

    def _valuta_uscita(self, mint: str, info: dict):
        """Decide e, se serve, LANCIA la vendita come task separato. Non
        attende l'esecuzione: il loop di monitoraggio deve restare libero di
        controllare le altre posizioni."""
        pos = self.risk.posizioni.get(mint)
        if not pos or mint in self._uscite_in_corso:
            return
        dec = self.risk.decisione_uscita(mint, info["prezzo"], info.get("change_5m"))
        if dec.azione == "HOLD":
            return
        self._uscite_in_corso.add(mint)
        asyncio.create_task(self._esegui_uscita(mint, info["prezzo"], dec))

    async def _esegui_uscita(self, mint: str, prezzo: float, dec):
        lock = self._lock_uscita.setdefault(mint, asyncio.Lock())
        try:
            async with lock:
                pos = self.risk.posizioni.get(mint)
                if not pos:
                    return
                residuo_prima = pos.quantita_raw
                qty = int(residuo_prima * dec.frazione)
                if qty <= 0:
                    return

                if dec.urgente:
                    # Stop loss / volatility stop: vendita secca, nessuna
                    # attesa del market calm.
                    res = await self.executor.vendi(mint, qty)
                else:
                    fx = await self._cambio_usd_eur()
                    quota = qty / max(pos.quantita_iniziale_raw, 1)
                    size_usd = quota * pos.sol_investiti * self.prezzo_sol_eur / max(fx, 0.01)
                    res = await self.executor.vendi_tranches(
                        mint, qty, self.market, size_usd, urgente=False)

                venduto = res.unita_vendute_raw
                if venduto <= 0:
                    log.error("Vendita %s (%s) fallita: %s — riprovo al prossimo ciclo",
                              pos.symbol, dec.motivo, res.errore)
                    return

                frazione_reale = min(1.0, venduto / max(residuo_prima, 1))
                pnl_pct = pos.pnl_pct(prezzo)
                # Costo base della sola parte venduta, calcolato sulla
                # quantità REALE rispetto all'originale: coerente con come è
                # stata dimensionata la posizione.
                costo_venduto_eur = (pos.sol_investiti * venduto
                                     / max(pos.quantita_iniziale_raw, 1)) * self.prezzo_sol_eur
                pnl_eur = costo_venduto_eur * pnl_pct

                # I tier del ladder vengono marcati SOLO qui, dopo la conferma:
                # un tier la cui vendita è fallita resta disponibile.
                breaker = self.risk.registra_uscita(
                    mint, pnl_eur, frazione_reale, dec.motivo,
                    tiers_eseguiti=dec.tiers_candidati if venduto > 0 else None)

                if mint not in self.risk.posizioni:
                    self.market.dimentica(mint)

                emoji = "🟢" if pnl_eur >= 0 else "🔴"
                await self.telegram.invia(
                    f"{emoji} <b>{pos.symbol}</b> — {dec.motivo}\n"
                    f"PnL: {pnl_eur:+.2f}€ ({pnl_pct * 100:+.1f}%)\n"
                    f"Venduto: {frazione_reale * 100:.0f}% del residuo\n"
                    f"Capitale tracciato: {self.risk.capitale_eur:.2f}€"
                )

                if frazione_reale < 0.999 and mint in self.risk.posizioni:
                    log.warning("⚠️ Vendita parziale su %s: %.0f%% del richiesto (%s). "
                                "Il residuo resta in posizione e verrà ritentato.",
                                pos.symbol, frazione_reale * 100, res.errore or "tranche incompleta")

                if breaker:
                    await self.telegram.invia(
                        f"🚨 <b>CIRCUIT BREAKER GIORNALIERO</b>: perdita giornaliera "
                        f"{self.risk.pnl_giornaliero_eur:+.2f}€ → pausa 24h. "
                        f"Nessun nuovo ingresso, le posizioni restano protette."
                    )
        except Exception as e:
            log.exception("Errore eseguendo l'uscita su %s: %s", mint, e)
        finally:
            self._uscite_in_corso.discard(mint)

    # ================= LOOP SORVEGLIANZA =================

    async def loop_sorveglianza(self):
        ultimo_comando = None
        while True:
            try:
                comando = leggi_comando_controllo()

                # Un "start" esplicito è l'unico modo per rimuovere lo STOP DI
                # SICUREZZA: l'utente riconosce il problema prima di ripartire.
                if comando == "run" and self.risk.in_pausa_fino == float("inf"):
                    self.risk.in_pausa_fino = 0.0
                    log.warning("▶️ Stop di sicurezza rimosso manualmente (comando 'start').")

                if comando != ultimo_comando:
                    log.warning("🎛️ Comando attivo: %s", comando.upper())
                    ultimo_comando = comando
                self.comando = comando

                await self._aggiorna_prezzo_sol()
                await self._controllo_sicurezza_valore()

                if comando == "run":
                    await self.agente.forse_esegui()
            except Exception as e:
                log.exception("Errore nel loop sorveglianza: %s", e)
            await asyncio.sleep(20.0)

    async def _controllo_sicurezza_valore(self):
        """Backstop indipendente dalla contabilità interna.

        CORREZIONE: la versione precedente confrontava il solo saldo SOL col
        capitale iniziale totale. Ma con posizioni aperte una quota legittima
        del capitale NON è in SOL: con 4 posizioni al 15% è il 60%. Il
        controllo scattava quindi su un bot perfettamente sano, imponendo una
        pausa INDEFINITA proprio mentre c'erano posizioni aperte che
        smettevano di essere protette. Ora si confronta il valore TOTALE
        (SOL + valore corrente delle posizioni), che è la grandezza che il
        floor vuole davvero misurare.
        """
        if CONFIG.mode != "live" or self.prezzo_sol_eur <= 0:
            return

        saldo_sol = await self.executor.saldo_sol()
        valore_eur = saldo_sol * self.prezzo_sol_eur

        if self.risk.posizioni:
            fx = await self._cambio_usd_eur()
            prezzi = await self.market.prezzi(list(self.risk.posizioni.keys()))
            for mint, pos in self.risk.posizioni.items():
                px = prezzi.get(mint, {}).get("prezzo")
                if px:
                    unita = pos.quantita_raw / (10 ** pos.decimals)
                    valore_eur += unita * px * fx
                else:
                    # Prezzo non disponibile: valuta al costo, non a zero.
                    # Valutarla a zero farebbe scattare il floor per un
                    # problema di API, non per una perdita reale.
                    valore_eur += pos.sol_investiti * self.prezzo_sol_eur

        floor_eur = self.risk.capitale_iniziale_eur_effettivo * CONFIG.risk.floor_sicurezza_pct
        if valore_eur < floor_eur:
            if self.risk.forza_pausa_sicurezza():
                log.critical(
                    "🚨 STOP DI SICUREZZA: valore totale wallet ≈%.2f€ (%.4f SOL + %d posizioni) "
                    "sotto la soglia minima %.2f€ (%.0f%% del capitale iniziale). Pausa automatica: "
                    "nessuna nuova apertura finché non lanci 'python control.py start'.",
                    valore_eur, saldo_sol, len(self.risk.posizioni), floor_eur,
                    CONFIG.risk.floor_sicurezza_pct * 100)
                await self.telegram.invia(
                    f"🚨 <b>STOP DI SICUREZZA</b>\n"
                    f"Valore totale wallet ≈{valore_eur:.2f}€ ({saldo_sol:.4f} SOL + "
                    f"{len(self.risk.posizioni)} posizioni), sotto la soglia minima {floor_eur:.2f}€ "
                    f"({CONFIG.risk.floor_sicurezza_pct * 100:.0f}% del capitale iniziale).\n"
                    f"Bot in pausa automatica — nessun nuovo ingresso, le posizioni restano protette.\n"
                    f"Manda PARTI quando hai verificato la situazione."
                )

    # ================= UTILITÀ =================

    async def _aggiorna_regime(self):
        regime = await self.mkt_sentiment.regime()
        self._regime_cache = regime
        self._mod_cache = MarketSentiment.modulatori(regime)
        try:
            with open("ultimo_regime.json", "w") as f:
                json.dump(regime, f)
        except OSError:
            pass

    async def _aggiorna_prezzo_sol(self):
        if time.time() - self._prezzo_sol_ts < CONFIG.prezzo_sol_refresh_sec and self.prezzo_sol_eur > 0:
            return
        prezzo = await self.market.prezzo_singolo(SOL_MINT)
        if prezzo:
            fx = await self._cambio_usd_eur()
            self.prezzo_sol_eur = prezzo * fx
            self._prezzo_sol_ts = time.time()

    async def _cambio_usd_eur(self) -> float:
        """Cambio USD→EUR reale (cache 6h). Fallback su 0.93."""
        if getattr(self, "_fx_ts", 0) and time.time() - self._fx_ts < 6 * 3600:
            return self._fx
        try:
            async with self.session.get(
                "https://api.frankfurter.app/latest?from=USD&to=EUR",
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    self._fx, self._fx_ts = float(data["rates"]["EUR"]), time.time()
                    return self._fx
        except Exception as e:
            log.warning("Cambio USD/EUR non disponibile (%s), uso fallback 0.93", e)
        self._fx, self._fx_ts = 0.93, time.time()
        return self._fx

    # ================= AVVIO =================

    async def _preflight_live(self):
        log.warning("⚠️  MODALITÀ LIVE: soldi veri a rischio!")

        # La pubkey derivata dalla chiave privata deve combaciare con quella attesa
        if CONFIG.api.wallet_public_key and self.executor.keypair:
            effettiva = str(self.executor.keypair.pubkey())
            if effettiva != CONFIG.api.wallet_public_key:
                log.critical("🚨 WALLET_PRIVATE_KEY non corrisponde a WALLET_PUBLIC_KEY! "
                             "Atteso=%s, effettivo=%s", CONFIG.api.wallet_public_key, effettiva)
                await self.telegram.invia(
                    f"🚨 <b>ATTENZIONE: wallet inatteso</b>\n"
                    f"La chiave privata corrisponde a <code>{effettiva}</code>, "
                    f"ma il wallet atteso è <code>{CONFIG.api.wallet_public_key}</code>.\n"
                    f"Verifica WALLET_PRIVATE_KEY in .env prima di fidarti di saldo e trade."
                )

        if not CONFIG.api.helius_api_key:
            log.critical("🚨 HELIUS_API_KEY mancante: senza RPC non si può confermare "
                         "nessuna transazione né leggere il saldo. Il bot non può operare in live.")
            raise RuntimeError("HELIUS_API_KEY mancante in modalità live")

        # Al PRIMISSIMO avvio live, ancora capitale e soglie al saldo VERO.
        if not self._stato_precedente:
            await self._aggiorna_prezzo_sol()
            saldo = await self.executor.saldo_sol(max_eta_sec=0)
            if self.prezzo_sol_eur > 0 and saldo > 0:
                saldo_eur = saldo * self.prezzo_sol_eur
                self.risk.imposta_capitale_iniziale_reale(saldo_eur)
                log.info("💰 Capitale iniziale rilevato dal wallet: %.2f€ (%.4f SOL)", saldo_eur, saldo)
                await self.telegram.invia(
                    f"💰 Primo avvio live: capitale ancorato al saldo reale del wallet "
                    f"<b>{saldo_eur:.2f}€</b> ({saldo:.4f} SOL).\n"
                    f"Floor di sicurezza: {CONFIG.risk.floor_sicurezza_pct * 100:.0f}% "
                    f"(≈{saldo_eur * CONFIG.risk.floor_sicurezza_pct:.2f}€)."
                )
            else:
                log.critical("🚨 Impossibile rilevare il saldo reale del wallet all'avvio "
                             "(SOL=%.4f, prezzo_sol_eur=%.2f). Il capitale resta il default di "
                             "config.py (%.2f€) — VERIFICA wallet e RPC prima di operare.",
                             saldo, self.prezzo_sol_eur, CONFIG.risk.capitale_iniziale_eur)
                await self.telegram.invia(
                    "🚨 Impossibile rilevare il saldo reale del wallet all'avvio. Il floor di "
                    "sicurezza userà il default di config.py, che potrebbe NON corrispondere al "
                    "capitale vero. Verifica wallet e RPC."
                )

    async def run(self):
        if CONFIG.mode == "live":
            await self._preflight_live()
        else:
            log.info("📝 MODALITÀ PAPER: nessuna transazione reale, nessun capitale a rischio.")

        await self._aggiorna_prezzo_sol()
        log.info("🚀 Bot avviato | modalità=%s | capitale=%.2f€ | discovery %.1fs · monitor %.1fs",
                 CONFIG.mode.upper(), self.risk.capitale_eur,
                 CONFIG.scan_interval_sec, CONFIG.monitor_interval_sec)
        await self.telegram.invia(
            f"🚀 Bot avviato | modalità={CONFIG.mode.upper()} | "
            f"capitale tracciato={self.risk.capitale_eur:.2f}€\n"
            f"Comandi: PARTI, PAUSA, STOP, STATO."
        )

        # I tre loop più il polling Telegram girano in parallelo e indipendenti.
        await asyncio.gather(
            self.loop_sorveglianza(),
            self.loop_discovery(),
            self.loop_monitor(),
            self.telegram.poll_comandi(),
        )


async def main():
    timeout = aiohttp.ClientTimeout(total=20)
    connector = aiohttp.TCPConnector(limit=60, ttl_dns_cache=300)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        bot = MemecoinBot(session)
        await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot fermato manualmente. Stato salvato in stato_bot.json")
