"""
market_sentiment.py — Sentiment AGGREGATO del mercato memecoin, 100% locale.

Differenza con sentiment.py:
  - sentiment.py         → "Questo specifico token ha una presenza social solida?"
  - market_sentiment.py  → "Il mercato memecoin/crypto nel suo complesso è in
                             euforia, normale, o in fase di panico/liquidazione?"

Fonti (entrambe pubbliche, gratuite, senza API key):
  1. Crypto Fear & Greed Index (alternative.me) — indice 0-100 aggregato da
     volatilità, momentum/volume, social media, dominance, trend di ricerca.
  2. Variazione SOL 24h (CoinGecko, endpoint pubblico) — il regime memecoin
     su Solana segue da vicino il prezzo di SOL stesso.

Nessuna delle due richiede ANTHROPIC_API_KEY: il regime di mercato non
dipende più da nessuna AI esterna, solo da dati di mercato pubblici.

Il regime di mercato NON blocca mai il bot del tutto: modula il
cluster_top_n e la size massima concessa. Un mercato in panico riduce
l'esposizione, non la azzera.

Uso tipico: chiamato una volta ogni ~30-60 minuti (cache), non ad ogni ciclo.
"""

import logging
import time

import aiohttp

log = logging.getLogger("market_sentiment")

FNG_URL = "https://api.alternative.me/fng/?limit=1"
SOL_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd&include_24hr_change=true"


class MarketSentiment:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.cache: dict | None = None
        self.cache_ts: float = 0.0
        self.ttl_sec = 45 * 60  # ricalcola al massimo ogni 45 minuti

    async def regime(self, forza_refresh: bool = False) -> dict:
        """Ritorna il regime corrente, usando la cache se ancora valida."""
        if not forza_refresh and self.cache and (time.time() - self.cache_ts) < self.ttl_sec:
            return self.cache

        risultato = await self._calcola_regime()
        self.cache = risultato
        self.cache_ts = time.time()
        return risultato

    async def _fear_greed(self) -> int | None:
        try:
            async with self.session.get(FNG_URL, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                return int(data["data"][0]["value"])
        except Exception as e:
            log.warning("Fear&Greed non disponibile: %s", e)
            return None

    async def _variazione_sol_24h(self) -> float | None:
        try:
            async with self.session.get(SOL_PRICE_URL, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                return float(data["solana"]["usd_24h_change"])
        except Exception as e:
            log.warning("Variazione SOL 24h non disponibile: %s", e)
            return None

    async def _calcola_regime(self) -> dict:
        fng, var_sol = await self._fear_greed(), await self._variazione_sol_24h()

        if fng is None and var_sol is None:
            return self._fallback()

        # Score 0-100 combinato: Fear&Greed pesa di più (è già un aggregato
        # multi-fattore), la variazione SOL 24h lo affina con un segnale
        # specifico Solana. Se una fonte manca, si usa solo l'altra.
        componenti = []
        if fng is not None:
            componenti.append((fng, 0.65))
        if var_sol is not None:
            # +15% in 24h → contributo massimo (100) | -15% → contributo minimo (0)
            score_sol = max(0.0, min(100.0, 50 + var_sol / 15 * 50))
            componenti.append((score_sol, 0.35))
        peso_tot = sum(p for _, p in componenti)
        score = round(sum(v * p for v, p in componenti) / peso_tot)

        if score >= 70:
            regime = "EUFORIA"
        elif score >= 45:
            regime = "NORMALE"
        elif score >= 25:
            regime = "CAUTELA"
        else:
            regime = "PANICO"

        note = f"Fear&Greed={fng if fng is not None else 'n/d'}, SOL 24h={var_sol:+.1f}%" if var_sol is not None else f"Fear&Greed={fng}"
        log.info("🌡️ Regime di mercato: %s (score %d) — %s", regime, score, note)
        return {"regime": regime, "score": score, "note": note, "eventi_rilevanti": []}

    @staticmethod
    def _fallback() -> dict:
        """Errore su entrambe le fonti = prudenza: regime NORMALE, nessuna modulazione."""
        return {"regime": "NORMALE", "score": 50, "note": "dati di mercato non disponibili (fallback)", "eventi_rilevanti": []}

    # ---------------- MODULAZIONE (usata dal bot) ----------------

    @staticmethod
    def modulatori(regime_data: dict) -> dict:
        """
        Traduce il regime in moltiplicatori concreti per il bot.
        Non azzera mai l'operatività: PANICO riduce, non blocca.
        """
        regime = regime_data.get("regime", "NORMALE")
        mappa = {
            "EUFORIA":  {"cluster_top_n_delta": 0,  "size_mult": 0.90, "min_momentum_extra": 5,
                        "nota": "euforia = rischio di comprare il top locale: leggermente più prudente"},
            "NORMALE":  {"cluster_top_n_delta": 0,  "size_mult": 1.00, "min_momentum_extra": 0,
                        "nota": "nessuna modulazione"},
            "CAUTELA":  {"cluster_top_n_delta": -1, "size_mult": 0.75, "min_momentum_extra": 10,
                        "nota": "riduci esposizione ed esigi momentum più forte"},
            "PANICO":   {"cluster_top_n_delta": -1, "size_mult": 0.50, "min_momentum_extra": 20,
                        "nota": "esposizione dimezzata, solo segnali molto forti"},
        }
        return mappa.get(regime, mappa["NORMALE"])
