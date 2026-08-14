"""
main.py — Orchestratore del bot. Loop principale h24.

Pipeline per ogni ciclo (~20 secondi):
  1. SCAN     → scanner.py trova nuovi pool
  2. FILTER   → filters.py applica i filtri anti-rug       ┐ eseguiti in
  3. SENTIMENT→ sentiment.py valuta la presenza social      ┘ parallelo tra loro
  4. ANALYZE  → local_analyzer.py calcola uno score 0-100 con formula
                quantitativa locale (nessuna chiamata AI esterna)
  5. EXECUTE  → executor.py compra via Jupiter (se score >= soglia)
  6. MONITOR  → risk_manager.py controlla SL/TP/trailing su ogni posizione

I candidati di ogni ciclo vengono valutati IN PARALLELO (fino a
CONFIG.max_candidati_paralleli insieme) invece che uno alla volta: con
sentiment/regime che usano web_search (timeout fino a 90s), la valutazione
sequenziale poteva far scivolare il ciclo reale ben oltre i 20s nominali.

Controllo manuale — vedi control.py:
  python control.py start | pausa | stop
Il bot legge control.json ad ogni ciclo:
  - run:   normale (ingressi + monitoraggio)
  - pausa: nessun nuovo ingresso, monitoraggio/protezione posizioni ATTIVI
  - stop:  tutto fermo, incluso il monitoraggio (posizioni non più protette)

Stop di sicurezza automatico: se il saldo SOL reale del wallet scende sotto
CONFIG.risk.floor_sicurezza_pct del capitale iniziale, il bot si mette in
pausa indefinita da solo (indipendentemente da cosa dice la contabilità
interna) finché non arriva un "python control.py start" esplicito.

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
from config import CONFIG
from executor import JupiterExecutor, LAMPORTS_PER_SOL
from filters import TokenFilter
from local_analyzer import LocalAnalyzer
from market_conditions import MarketConditions
from market_sentiment import MarketSentiment
from risk_manager import Posizione, RiskManager
from scanner import PoolScanner
from sentiment import SentimentAnalyzer
from telegram_bot import TelegramNotifier
from timing import momentum_score, score_composito

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-8s | %(levelname)-7s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("bot.log")],
)
log = logging.getLogger("main")


def leggi_comando_controllo() -> str:
    """Legge control.json (scritto da control.py). Default 'run' se assente/corrotto."""
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
        self._stato_precedente_trovato = self.risk.carica_stato()
        self.agente = AgentSupervisor(session, self.risk)
        self.telegram = TelegramNotifier(session)
        self.session = session
        self.prezzo_sol_eur = 0.0
        # Clustering: buffer di candidati (candidato, score_composito, ts)
        self.cluster: list[tuple] = []
        self.cluster_apertura_ts = 0.0
        self._sem_candidati = asyncio.Semaphore(CONFIG.max_candidati_paralleli)

    # ---------------- CICLO DI INGRESSO ----------------

    async def _valuta_candidato(self, c, mod: dict, regime_data: dict) -> tuple | None:
        """Valuta un singolo candidato. Ritorna None se scartato, altrimenti
        la tupla da mettere nel cluster. Filtro e sentiment sono indipendenti
        tra loro (nessuno dei due dipende dall'output dell'altro): li lancio
        in parallelo per dimezzare la latenza di rete di questo stadio."""
        async with self._sem_candidati:
            mom = momentum_score(c)
            soglia_momentum = CONFIG.filters.min_momentum_score + mod["min_momentum_extra"]
            if mom < soglia_momentum:
                log.debug("Momentum insufficiente per %s: %.0f (soglia %.0f, regime %s)",
                         c.symbol, mom, soglia_momentum, regime_data.get("regime"))
                return None

            fr, sent = await asyncio.gather(self.filtro.valuta(c), self.sentiment.analizza(c))

            if not fr.passed:
                return None

            if sent.get("sentiment_score", 0) < CONFIG.filters.min_sentiment_score:
                log.info("Sentiment insufficiente per %s: %s (%s)", c.symbol,
                         sent.get("sentiment_score"), sent.get("hype_type"))
                return None

            analisi = {"score": 75}
            if CONFIG.usa_analisi_locale:
                analisi = await self.analizzatore.analizza(c, fr)
                if analisi.get("decisione") != "COMPRA" or analisi.get("score", 0) < CONFIG.min_claude_score:
                    log.info("Analisi locale scarta %s: %s", c.symbol, analisi.get("motivazione"))
                    return None

            comp = score_composito(analisi.get("score", 0), sent.get("sentiment_score", 0), mom)
            log.info("🧺 %s valutato con score composito %.0f", c.symbol, comp)
            return (c, comp, analisi.get("score", 0), sent.get("sentiment_score", 0), mom)

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

        # Valutazione IN PARALLELO (limitata da max_candidati_paralleli) invece
        # che uno alla volta: riduce di molto il tempo reale del ciclo quando
        # arrivano più candidati insieme nello stesso scan.
        risultati = await asyncio.gather(*[self._valuta_candidato(c, mod, regime_data) for c in candidati])
        nuovi = [r for r in risultati if r is not None]
        if nuovi and not self.cluster:
            self.cluster_apertura_ts = _t.time()
        self.cluster.extend(nuovi)

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
            if CONFIG.mode == "live":
                await self.telegram.invia(f"⚠️ Acquisto <b>{c.symbol}</b> fallito: {res.errore}")
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
        riga_score = f"\nScore composito: {score_composito:.0f}" if score_composito is not None else ""
        await self.telegram.invia(
            f"📈 Aperta posizione <b>{c.symbol}</b>\n"
            f"Prezzo: ${c.price_usd:.8f}\n"
            f"Investiti: {sol_amount:.4f} SOL (≈{size_eur:.2f}€){riga_score}"
        )

    # ---------------- CICLO DI MONITORAGGIO ----------------

    async def ciclo_monitoraggio(self):
        mints = list(self.risk.posizioni.keys())
        # Al massimo 3 posizioni contemporanee (config): il parallelismo qui
        # non è critico per il volume, ma evita che una posizione lenta a
        # rispondere (rete, RPC) ritardi il controllo delle altre.
        await asyncio.gather(*[self._gestisci_posizione(mint) for mint in mints])

    async def _gestisci_posizione(self, mint: str):
        snap = await self.market.snapshot(mint)
        if not snap:
            return
        prezzo = float(snap.get("priceUsd") or 0)
        change5m = float((snap.get("priceChange") or {}).get("m5") or 0)
        if prezzo <= 0:
            return
        azione, frazione_richiesta = self.risk.decisione_uscita(mint, prezzo, change5m)
        if azione == "HOLD":
            return

        pos = self.risk.posizioni.get(mint)
        if not pos:
            return
        residuo_prima = pos.quantita_raw
        qty = int(residuo_prima * frazione_richiesta)

        # Stop loss = urgenza: vendita secca immediata, niente tranches.
        # Ladder/trailing/time exit = vendita parzializzata calm-aware.
        urgente = pos.pnl_pct(prezzo) <= CONFIG.risk.stop_loss_pct
        if urgente:
            res = await self.executor.vendi(mint, qty)
        else:
            size_usd = qty / max(pos.quantita_iniziale_raw, 1) * pos.sol_investiti * self.prezzo_sol_eur / 0.93
            res = await self.executor.vendi_tranches(mint, qty, self.market, size_usd)

        # ---- Contabilità basata SOLO su ciò che è stato realmente venduto ----
        # `res.unita_vendute_raw` riflette l'esecuzione reale (confermata
        # on-chain), non la frazione richiesta: se una tranche fallisce a metà
        # strada, qui si aggiorna la posizione solo per la parte davvero
        # venduta — niente più posizioni "chiuse sulla carta" con token reali
        # dimenticati nel wallet.
        venduto_raw = res.unita_vendute_raw
        if venduto_raw <= 0:
            if not res.ok:
                log.error("Vendita %s fallita: %s — riprovo al prossimo ciclo", pos.symbol, res.errore)
            return

        frazione_reale = min(1.0, venduto_raw / max(residuo_prima, 1))
        pnl_pct = pos.pnl_pct(prezzo)
        valore_venduto_eur = pos.sol_investiti * frazione_reale * self.prezzo_sol_eur
        pnl_eur = valore_venduto_eur * pnl_pct
        motivo = "stop_loss" if urgente else ("ladder" if pos.tiers_eseguiti else "trailing_o_time")
        breaker_scattato = self.risk.chiudi(mint, pnl_eur, frazione_reale, motivo=motivo)

        emoji = "🟢" if pnl_eur >= 0 else "🔴"
        await self.telegram.invia(
            f"{emoji} Chiusa posizione <b>{pos.symbol}</b> ({motivo})\n"
            f"PnL: {pnl_eur:+.2f}€ ({pnl_pct * 100:+.0f}%)\n"
            f"Frazione venduta: {frazione_reale * 100:.0f}%\n"
            f"Capitale tracciato: {self.risk.capitale_eur:.2f}€"
        )

        if frazione_reale < 0.999:
            log.warning(
                "⚠️ Vendita parziale su %s: venduto %.0f%% di quanto richiesto (%s). "
                "Il residuo resta in posizione e verrà ritentato al prossimo ciclo.",
                pos.symbol, frazione_reale * 100, res.errore or "tranche incompleta",
            )
            await self.telegram.invia(
                f"⚠️ Vendita parziale su <b>{pos.symbol}</b>: venduto solo {frazione_reale * 100:.0f}% "
                f"del richiesto. Il residuo resta protetto e verrà ritentato."
            )

        if breaker_scattato:
            await self.telegram.invia(
                f"🚨 <b>CIRCUIT BREAKER GIORNALIERO</b>: perdita giornaliera {self.risk.pnl_giornaliero_eur:+.2f}€ "
                f"→ bot in pausa per 24h. Nessun nuovo ingresso, le posizioni restano protette."
            )

    # ---------------- STOP DI SICUREZZA (saldo reale on-chain) ----------------

    async def controllo_sicurezza_saldo(self):
        """Backstop indipendente dalla contabilità interna: se il saldo SOL
        REALE del wallet scende sotto floor_sicurezza_pct del capitale
        iniziale, forza una pausa indefinita a prescindere da cosa dice
        stato_bot.json. Serve a coprire bug di contabilità non ancora noti,
        non solo quelli già corretti."""
        if CONFIG.mode != "live" or self.prezzo_sol_eur <= 0:
            return
        saldo_sol = await self.executor.saldo_sol()
        saldo_eur = saldo_sol * self.prezzo_sol_eur
        # Ancorato al capitale REALE rilevato al primo avvio live (self.risk.
        # capitale_iniziale_eur_effettivo), non al default fisso di config.py:
        # se hai depositato più o meno di 100€, il floor si adegua di conseguenza.
        floor_eur = self.risk.capitale_iniziale_eur_effettivo * CONFIG.risk.floor_sicurezza_pct
        if saldo_eur < floor_eur:
            if self.risk.forza_pausa_sicurezza():
                log.critical(
                    "🚨 STOP DI SICUREZZA: saldo reale wallet ≈%.2f€ (%.4f SOL) sotto la soglia "
                    "minima %.2f€ (%.0f%% del capitale iniziale). Bot in pausa automatica — "
                    "nessuna nuova apertura finché non lanci 'python control.py start'.",
                    saldo_eur, saldo_sol, floor_eur, CONFIG.risk.floor_sicurezza_pct * 100,
                )
                await self.telegram.invia(
                    f"🚨 <b>STOP DI SICUREZZA</b>\n"
                    f"Saldo reale wallet ≈{saldo_eur:.2f}€ ({saldo_sol:.4f} SOL), sotto la soglia minima "
                    f"{floor_eur:.2f}€ ({CONFIG.risk.floor_sicurezza_pct * 100:.0f}% del capitale iniziale).\n"
                    f"Bot in pausa automatica — nessun nuovo ingresso.\n"
                    f"Manda PARTI quando hai verificato la situazione."
                )

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
        if CONFIG.mode == "live":
            log.warning("⚠️  MODALITÀ LIVE: soldi veri a rischio!")
            # Al PRIMISSIMO avvio live (nessuno stato precedente su disco) ancora
            # capitale iniziale, circuit breaker giornaliero e floor di sicurezza
            # al saldo VERO del wallet, non al default hardcoded in config.py.
            # Se già esiste uno stato_bot.json, NON si tocca: si perderebbe la
            # contabilità accumulata dei trade già fatti.
            if not self._stato_precedente_trovato:
                await self._aggiorna_prezzo_sol()
                saldo_sol = await self.executor.saldo_sol()
                if self.prezzo_sol_eur > 0 and saldo_sol > 0:
                    saldo_eur = saldo_sol * self.prezzo_sol_eur
                    self.risk.imposta_capitale_iniziale_reale(saldo_eur)
                    log.info("💰 Capitale iniziale rilevato dal wallet reale: %.2f€ (%.4f SOL)", saldo_eur, saldo_sol)
                    await self.telegram.invia(
                        f"💰 Primo avvio live: capitale iniziale ancorato al saldo reale del wallet: "
                        f"<b>{saldo_eur:.2f}€</b> ({saldo_sol:.4f} SOL).\n"
                        f"Floor di sicurezza: {CONFIG.risk.floor_sicurezza_pct * 100:.0f}% di questo valore "
                        f"(≈{saldo_eur * CONFIG.risk.floor_sicurezza_pct:.2f}€) — sotto questa soglia il bot si ferma da solo."
                    )
                else:
                    log.critical(
                        "🚨 Impossibile rilevare il saldo reale del wallet all'avvio (SOL=%.4f, prezzo_sol_eur=%.2f). "
                        "Il capitale iniziale resta il default di config.py (%.2f€) — VERIFICA wallet/RPC prima di operare.",
                        saldo_sol, self.prezzo_sol_eur, CONFIG.risk.capitale_iniziale_eur,
                    )
                    await self.telegram.invia(
                        "🚨 Impossibile rilevare il saldo reale del wallet all'avvio. Il floor di sicurezza userà "
                        "il default di config.py, che potrebbe NON corrispondere al capitale vero. Verifica wallet/RPC."
                    )
        log.info("🚀 Bot avviato | modalità=%s | capitale=%.2f EUR", CONFIG.mode.upper(), self.risk.capitale_eur)
        asyncio.create_task(self.telegram.poll_comandi())
        await self.telegram.invia(
            f"🚀 Bot avviato | modalità={CONFIG.mode.upper()} | capitale tracciato={self.risk.capitale_eur:.2f}€\n"
            f"Comandi: PARTI, PAUSA, STOP, STATO."
        )
        ultimo_comando_loggato = None
        while True:
            try:
                comando = leggi_comando_controllo()

                # Un comando "start" esplicito è l'unico modo per rimuovere lo
                # STOP DI SICUREZZA (pausa indefinita) — l'utente riconosce
                # così il problema prima di far ripartire il bot.
                if comando == "run" and self.risk.in_pausa_fino == float("inf"):
                    self.risk.in_pausa_fino = 0.0
                    log.warning("▶️ Stop di sicurezza rimosso manualmente (comando 'start').")

                if comando != ultimo_comando_loggato:
                    # La conferma via Telegram (se il comando arriva da lì) la manda
                    # già _gestisci_messaggio(): qui basta il log, per non duplicare
                    # il messaggio in chat.
                    log.warning("🎛️ Comando attivo: %s", comando.upper())
                    ultimo_comando_loggato = comando

                if comando == "stop":
                    # Tutto fermo, incluso il monitoraggio: nessuna azione automatica.
                    await asyncio.sleep(CONFIG.scan_interval_sec)
                    continue

                await self._aggiorna_prezzo_sol()
                await self.controllo_sicurezza_saldo()

                if comando == "pausa":
                    # Nessun nuovo ingresso, ma le posizioni aperte restano protette.
                    await self.ciclo_monitoraggio()
                else:
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
