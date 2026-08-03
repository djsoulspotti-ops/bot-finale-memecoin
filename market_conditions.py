"""
market_conditions.py — Market Calm Score (0-100) per le uscite parzializzate.

Su DEX Solana non esiste un order book classico con bid/ask, quindi lo
"spread" viene approssimato con proxy on-chain disponibili via DexScreener:

  1. STABILITÀ (40%)  → |variazione prezzo 5m|: bassa = mercato calmo
  2. LIQUIDITÀ  (35%) → rapporto liquidità/volume 5m: alto = ordini assorbiti bene
  3. EQUILIBRIO (25%) → buy/sell ratio vicino a 1: nessun panico direzionale

Calm > 60  → esegui la tranche di vendita
Calm 40-60 → attendi il prossimo check
Calm < 40  → mercato turbolento: attendi (con timeout di sicurezza)
"""

import logging
import math

import aiohttp

from config import CONFIG

log = logging.getLogger("market")


class MarketConditions:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def snapshot(self, mint: str) -> dict | None:
        url = f"{CONFIG.api.dexscreener_url}/tokens/v1/solana/{mint}"
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return None
                pairs = await r.json()
                if not pairs:
                    return None
                return pairs[0]
        except Exception as e:
            log.error("Errore snapshot %s: %s", mint, e)
            return None

    async def calm_score(self, mint: str) -> float:
        """Ritorna 0-100. In caso di dati mancanti: 0 (prudenziale)."""
        pair = await self.snapshot(mint)
        if not pair:
            return 0.0

        change_5m = abs(float((pair.get("priceChange") or {}).get("m5") or 0))
        vol_5m = float((pair.get("volume") or {}).get("m5") or 0)
        liq = float((pair.get("liquidity") or {}).get("usd") or 0)
        txns = (pair.get("txns") or {}).get("m5") or {}
        buys, sells = int(txns.get("buys") or 0), int(txns.get("sells") or 0)

        # 1. Stabilità: 0% var → 100 | ≥25% var → 0
        stabilita = max(0.0, 100.0 - change_5m * 4.0)

        # 2. Liquidità relativa: liq/vol alto = i tuoi ordini pesano poco
        #    ratio ≥ 5 → 100 | ratio 0 → 0 (scala log per smussare)
        ratio = liq / max(vol_5m, 1.0)
        liquidita = min(100.0, 100.0 * math.log1p(ratio) / math.log1p(5.0))

        # 3. Equilibrio flussi: buy/sell vicino a 1 = 100, sbilanciato = 0
        tot = buys + sells
        if tot < 3:
            equilibrio = 30.0  # quasi nessuna transazione: mercato illiquido, non "calmo"
        else:
            sbilancio = abs(buys - sells) / tot          # 0 = perfetto, 1 = tutto un lato
            equilibrio = 100.0 * (1.0 - sbilancio)

        score = 0.40 * stabilita + 0.35 * liquidita + 0.25 * equilibrio
        log.debug("Calm %s = %.0f (stab %.0f, liq %.0f, eq %.0f)", mint[:6], score, stabilita, liquidita, equilibrio)
        return round(score, 1)

    async def slippage_stimato_ok(self, mint: str, size_usd: float, max_pct: float = 5.0) -> bool:
        """Stima grezza dell'impatto: size vs liquidità del pool."""
        pair = await self.snapshot(mint)
        if not pair:
            return False
        liq = float((pair.get("liquidity") or {}).get("usd") or 0)
        if liq <= 0:
            return False
        impatto_pct = (size_usd / liq) * 100 * 2  # fattore 2: impatto AMM non lineare
        return impatto_pct <= max_pct
