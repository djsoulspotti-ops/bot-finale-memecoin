"""
main.py — Orchestratore del bot. Loop principale h24.

Pipeline per ogni ciclo (~20 secondi):
  1. SCAN     → scanner.py trova nuovi pool
  2. FILTER   → filters.py applica i filtri anti-rug
  3. ANALYZE  → claude_analyzer.py chiede a Claude uno score 0-100
  4. EXECUTE  → executor.py compra via Jupiter (se score >= soglia)
  5. MONITOR  → risk_manager.py controlla SL/TP/trailing su ogni posizione

Avvio:   python main.py
Il bot parte in PAPER MODE (simulazione). Per il live: BOT_MODE=live in .env
— ma solo dopo ALMENO 2 settimane di paper trading con risultati verificati.
"""

import asyncio
import json
import logging
import sys

import aiohttp

from agent import AgentSupervisor
from claude_analyzer import ClaudeAnalyzer
from config import CONFIG
from executor import JupiterExecutor, LAMPORTS_PER_SOL
from filters import TokenFilter
from market_conditions import MarketConditions
from market_sentiment import MarketSentiment
from risk_manager import Posizione, RiskManager
from scanner import PoolScanner
from sentiment import SentimentAnalyzer
from timing import momentum_score, score_composito

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-8s | %(levelname)-7s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("bot.log")],
)
log = logging.getLogger("main")


class MemecoinBot:
    def __init__(self, session: aiohttp.ClientSession):
        self.scanner = PoolScanner(session)
        self.filtro = TokenFilter(session)
        self.claude = ClaudeAnalyzer(session)
        self.sentiment = SentimentAnalyzer(session)
        self.market = MarketConditions(session)
        self.mkt_sentiment = MarketSentiment(session)
        self.executor = JupiterExecutor(session)
        self.risk = RiskManager()
        self.risk.carica_stato()
        self.agente = AgentSupervisor(session, self.risk)
        self.session = session
        self.prezzo_sol_eur = 0.0
        # Clustering: buffer di candidati (candidato, score_composito, ts)
        self.cluster: list[tuple] = []
        self.cluster_apertura_ts = 0.0

    # ---------------- CICLO DI INGRESSO ----------------

    async def ciclo_ingresso(self):
        import time as _t
        ok, motivo = self.risk.puo_aprire()
        if not ok:
            log.debug("Ingresso bloccato: %s", motivo)
            return

        # Regime di mercato aggregato (cache ~45 min, non blocca mai il bot del tutto)
        regime_data = await self.mkt_sentiment.regime()
        mod = MarketSentiment.modulatori(regime_data)
        try:
            with open("ultimo_regime.json", "w") as f:
                json.dump(regime_data, f)
        except Exception:
            pass

        candidati = await self.scanner.scansiona_nuovi_pool()
        for c in candidati:
            # Gate momentum (gratis, prima di tutto: taglia i token morti subito)
            mom = momentum_score(c)
            soglia_momentum = CONFIG.filters.min_momentum_score + mod["min_momentum_extra"]
            if mom < soglia_momentum:
                log.debug("Momentum insufficiente per %s: %.0f (soglia %.0f, regime %s)",
                         c.symbol, mom, soglia_momentum, regime_data.get("regime"))
                continue

            # Filtri hard
            fr = await self.filtro.valuta(c)
            if not fr.passed:
                continue

            # Gate sentiment social
            sent = await self.sentiment.analizza(c)
            if sent.get("sentiment_score", 0) < CONFIG.filters.min_sentiment_score:
                log.info("Sentiment insufficiente per %s: %s (%s)", c.symbol,
                         sent.get("sentiment_score"), sent.get("hype_type"))
                continue
            if CONFIG.filters.scarta_se_shill and sent.get("hype_type") == "shill":
                log.info("Shill coordinato rilevato su %s → scarto", c.symbol)
                continue

            # Analisi Claude
            analisi = {"score": 75}
            if CONFIG.usa_analisi_claude:
                analisi = await self.claude.analizza(c, fr)
                if analisi.get("decisione") != "COMPRA" or analisi.get("score", 0) < CONFIG.min_claude_score:
                    log.info("Claude scarta %s: %s", c.symbol, analisi.get("motivazione"))
                    continue

            # Nel CLUSTER, non compra subito: aspetta la finestra e prendi i migliori
            comp = score_composito(analisi.get("score", 0), sent.get("sentiment_score", 0), mom)
            if not self.cluster:
                self.cluster_apertura_ts = _t.time()
            self.cluster.append((c, comp, analisi.get("score", 0), sent.get("sentiment_score", 0), mom))
            log.info("🧺 %s in cluster con score composito %.0f (%d nel buffer)", c.symbol, comp, len(self.cluster))

        # Finestra cluster scaduta → compra i top N (modulati dal regime di mercato)
        if self.cluster and _t.time() - self.cluster_apertura_ts >= CONFIG.risk.cluster_buffer_min * 60:
            self.cluster.sort(key=lambda x: x[1], reverse=True)
            top_n = max(1, CONFIG.risk.cluster_top_n + mod["cluster_top_n_delta"])
            migliori = self.cluster[:top_n]
            log.info("🏁 Cluster chiuso: %d candidati, compro i top %d (regime %s): %s",
                     len(self.cluster), len(migliori), regime_data.get("regime"),
                     [(c.symbol, s) for c, s, *_ in migliori])
            self.cluster = []
            for c, comp, claude_s, sent_s, mom_s in migliori:
                ok, _ = self.risk.puo_aprire()
                if not ok:
                    break
                # RIVALIDAZIONE FRESCA: i dati del candidato hanno fino a 7+ minuti.
                # Ricontrolla prezzo e condizioni ADESSO, prima di comprare.
                snap = await self.market.snapshot(c.mint)
                if not snap:
                    log.info("⏭️ %s: snapshot fresco non disponibile, salto", c.symbol)
                    continue
                prezzo_fresco = float(snap.get("priceUsd") or 0)
                if prezzo_fresco <= 0:
                    continue
                variazione = (prezzo_fresco - c.price_usd) / c.price_usd
                # Se nel frattempo è pumpato oltre +25%: stai comprando il top del pump → salta
                if variazione > 0.25:
                    log.info("⏭️ %s: +%.0f%% dallo scoring, rischio top del pump → salto", c.symbol, variazione * 100)
                    continue
                # Se è crollato oltre -20%: il segnale è morto → salta
                if variazione < -0.20:
                    log.info("⏭️ %s: %.0f%% dallo scoring, segnale decaduto → salto", c.symbol, variazione * 100)
                    continue
                # Aggiorna il prezzo d'entrata al valore FRESCO (statistiche pulite)
                c.price_usd = prezzo_fresco
                await self._apri_posizione(c, comp, size_mult=mod["size_mult"],
                                           claude_score=claude_s, sentiment_score=sent_s,
                                           momentum_score=mom_s, regime=regime_data.get("regime"))

    async def _apri_posizione(self, c, score_composito: float | None = None, size_mult: float = 1.0,
                              claude_score: float | None = None, sentiment_score: float | None = None,
                              momentum_score: float | None = None, regime: str | None = None):
        size_eur = self.risk.size_posizione_eur(score_composito) * size_mult
        await self._aggiorna_prezzo_sol()
        if self.prezzo_sol_eur <= 0:
            log.error("Prezzo SOL non disponibile, salto")
            return
        sol_amount = size_eur / self.prezzo_sol_eur

        res = await self.executor.compra(c.mint, sol_amount)
        if not res.ok:
            log.error("Acquisto %s fallito: %s", c.symbol, res.errore)
            return

        qty = int(res.output_amount)
        self.risk.apri(Posizione(
            mint=c.mint, symbol=c.symbol,
            prezzo_entrata=c.price_usd,
            quantita_raw=qty,
            quantita_iniziale_raw=qty,
            sol_investiti=sol_amount,
            claude_score=claude_score,
            sentiment_score=sentiment_score,
            momentum_score=momentum_score,
            score_composito=score_composito,
            regime_entrata=regime,
        ))

    # ---------------- CICLO DI MONITORAGGIO ----------------

    async def ciclo_monitoraggio(self):
        for mint in list(self.risk.posizioni.keys()):
            snap = await self.market.snapshot(mint)
            if not snap:
                continue
            prezzo = float(snap.get("priceUsd") or 0)
            change5m = float((snap.get("priceChange") or {}).get("m5") or 0)
            if prezzo <= 0:
                continue
            azione, frazione = self.risk.decisione_uscita(mint, prezzo, change5m)
            if azione == "HOLD":
                continue

            pos = self.risk.posizioni[mint]
            qty = int(pos.quantita_raw * frazione)
            size_usd = qty / max(pos.quantita_iniziale_raw, 1) * pos.sol_investiti * self.prezzo_sol_eur / 0.93

            # Stop loss = urgenza: vendita secca immediata, niente tranches.
            # Ladder/trailing/time exit = vendita parzializzata calm-aware.
            urgente = self.risk.posizioni[mint].pnl_pct(prezzo) <= CONFIG.risk.stop_loss_pct
            if urgente:
                res = await self.executor.vendi(mint, qty)
            else:
                res = await self.executor.vendi_tranches(mint, qty, self.market, size_usd)
            if res.ok:
                pnl_pct = pos.pnl_pct(prezzo)
                valore_venduto_eur = pos.sol_investiti * frazione * self.prezzo_sol_eur
                pnl_eur = valore_venduto_eur * pnl_pct
                motivo = "stop_loss" if urgente else ("ladder" if pos.tiers_eseguiti else "trailing_o_time")
                self.risk.chiudi(mint, pnl_eur, frazione, motivo=motivo)
            else:
                log.error("Vendita %s fallita: %s — riprovo al prossimo ciclo", pos.symbol, res.errore)

    async def _prezzo_corrente(self, mint: str) -> float | None:
        url = f"{CONFIG.api.dexscreener_url}/tokens/v1/solana/{mint}"
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    pairs = await r.json()
                    if pairs:
                        return float(pairs[0].get("priceUsd") or 0)
        except Exception as e:
            log.error("Errore prezzo %s: %s", mint, e)
        return None

    async def _aggiorna_prezzo_sol(self):
        prezzo = await self._prezzo_corrente("So11111111111111111111111111111111111111112")
        if prezzo:
            fx = await self._cambio_usd_eur()
            self.prezzo_sol_eur = prezzo * fx

    async def _cambio_usd_eur(self) -> float:
        """Cambio USD→EUR reale (cache 6h). Fallback su 0.93 se l'API non risponde."""
        import time as _t
        if hasattr(self, "_fx_cache") and _t.time() - self._fx_cache_ts < 6 * 3600:
            return self._fx_cache
        try:
            async with self.session.get(
                "https://api.frankfurter.app/latest?from=USD&to=EUR",
                timeout=__import__("aiohttp").ClientTimeout(total=10),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    fx = float(data["rates"]["EUR"])
                    self._fx_cache, self._fx_cache_ts = fx, _t.time()
                    return fx
        except Exception as e:
            log.warning("Cambio USD/EUR non disponibile (%s), uso fallback 0.93", e)
        self._fx_cache, self._fx_cache_ts = 0.93, _t.time()
        return 0.93

    # ---------------- LOOP PRINCIPALE ----------------

    async def run(self):
        log.info("🚀 Bot avviato | modalità=%s | capitale=%.2f EUR", CONFIG.mode.upper(), self.risk.capitale_eur)
        if CONFIG.mode == "live":
            log.warning("⚠️  MODALITÀ LIVE: soldi veri a rischio!")
        while True:
            try:
                await asyncio.gather(self.ciclo_ingresso(), self.ciclo_monitoraggio())
                await self.agente.forse_esegui()   # supervisore agentico: ogni 6h
            except Exception as e:
                log.exception("Errore nel ciclo principale: %s", e)
            await asyncio.sleep(CONFIG.scan_interval_sec)


async def main():
    async with aiohttp.ClientSession() as session:
        bot = MemecoinBot(session)
        await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot fermato manualmente. Stato salvato in stato_bot.json")
