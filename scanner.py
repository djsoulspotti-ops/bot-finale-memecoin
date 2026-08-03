"""
scanner.py — Rileva nuovi token/pool su Solana.

Strategia: interroga DexScreener (endpoint token-profiles e pairs più recenti)
per trovare pool nati da poco su Raydium/Pump.fun/Meteora.
Restituisce candidati grezzi che verranno poi passati a filters.py.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

import aiohttp

from config import CONFIG

log = logging.getLogger("scanner")


@dataclass
class TokenCandidate:
    mint: str                    # address del token
    symbol: str
    name: str
    pair_address: str
    dex: str                     # raydium / pumpswap / meteora
    liquidity_usd: float
    volume_5m_usd: float
    price_usd: float
    market_cap_usd: float
    pair_created_at: float       # timestamp ms
    price_change_5m: float = 0.0
    buys_5m: int = 0
    sells_5m: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def eta_minuti(self) -> float:
        return (time.time() * 1000 - self.pair_created_at) / 60_000

    @property
    def buy_sell_ratio(self) -> float:
        return self.buys_5m / max(self.sells_5m, 1)


class PoolScanner:
    """Scansiona DexScreener alla ricerca di pool Solana recenti."""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.visti: set[str] = set()   # mint già processati (no duplicati)

    async def _get(self, url: str) -> dict | list | None:
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.json()
                log.warning("DexScreener HTTP %s su %s", r.status, url)
        except Exception as e:
            log.error("Errore richiesta %s: %s", url, e)
        return None

    async def scansiona_nuovi_pool(self) -> list[TokenCandidate]:
        """
        1. Prende gli ultimi token profile boostati/nuovi su Solana
        2. Per ognuno recupera i dati del pair principale
        3. Scarta tutto ciò che è già stato visto
        """
        candidati: list[TokenCandidate] = []

        # Endpoint: ultimi token profiles (contiene i token appena listati/promossi)
        data = await self._get(f"{CONFIG.api.dexscreener_url}/token-profiles/latest/v1")
        if not data:
            return candidati

        mints_solana = [
            item["tokenAddress"]
            for item in data
            if item.get("chainId") == "solana" and item.get("tokenAddress") not in self.visti
        ][:30]

        if not mints_solana:
            return candidati

        # Batch fetch dei pair (max 30 address per richiesta)
        joined = ",".join(mints_solana)
        pairs_data = await self._get(f"{CONFIG.api.dexscreener_url}/tokens/v1/solana/{joined}")
        if not pairs_data:
            return candidati

        for pair in pairs_data:
            try:
                cand = self._parse_pair(pair)
                if cand and cand.mint not in self.visti:
                    self.visti.add(cand.mint)
                    candidati.append(cand)
            except (KeyError, TypeError, ValueError) as e:
                log.debug("Pair scartato per parsing: %s", e)

        log.info("Scansione completata: %d nuovi candidati", len(candidati))
        return candidati

    def _parse_pair(self, pair: dict) -> TokenCandidate | None:
        if pair.get("chainId") != "solana":
            return None
        liq = float((pair.get("liquidity") or {}).get("usd") or 0)
        vol5m = float((pair.get("volume") or {}).get("m5") or 0)
        txns5m = (pair.get("txns") or {}).get("m5") or {}
        return TokenCandidate(
            mint=pair["baseToken"]["address"],
            symbol=pair["baseToken"].get("symbol", "?"),
            name=pair["baseToken"].get("name", "?"),
            pair_address=pair.get("pairAddress", ""),
            dex=pair.get("dexId", "?"),
            liquidity_usd=liq,
            volume_5m_usd=vol5m,
            price_usd=float(pair.get("priceUsd") or 0),
            market_cap_usd=float(pair.get("marketCap") or 0),
            pair_created_at=float(pair.get("pairCreatedAt") or 0),
            price_change_5m=float((pair.get("priceChange") or {}).get("m5") or 0),
            buys_5m=int(txns5m.get("buys") or 0),
            sells_5m=int(txns5m.get("sells") or 0),
            raw=pair,
        )


async def _test():
    logging.basicConfig(level=logging.INFO)
    async with aiohttp.ClientSession() as s:
        scanner = PoolScanner(s)
        risultati = await scanner.scansiona_nuovi_pool()
        for c in risultati[:5]:
            print(f"{c.symbol:10s} liq=${c.liquidity_usd:,.0f} età={c.eta_minuti:.0f}min dex={c.dex}")


if __name__ == "__main__":
    asyncio.run(_test())
