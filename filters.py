"""
filters.py — Filtri anti-rug (hard filters).

Un token deve passare TUTTI i controlli prima di arrivare all'analisi AI.
Fonti dati:
  - DexScreener (già nel candidato): liquidità, volume, età, txns
  - RugCheck API: mint/freeze authority, LP lock, concentrazione holder
La stragrande maggioranza dei token nuovi (>95%) viene scartata qui.
"""

import logging
from dataclasses import dataclass, field

import aiohttp

from config import CONFIG
from scanner import TokenCandidate

log = logging.getLogger("filters")


@dataclass
class FilterResult:
    passed: bool
    motivi_scarto: list[str] = field(default_factory=list)
    rugcheck_score: int | None = None
    top10_holder_pct: float | None = None
    mint_revocato: bool | None = None
    freeze_revocato: bool | None = None
    lp_bloccata: bool | None = None


class TokenFilter:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.f = CONFIG.filters

    async def valuta(self, c: TokenCandidate) -> FilterResult:
        res = FilterResult(passed=True)

        # ---- 1. Filtri quantitativi (dati già disponibili) ----
        if c.liquidity_usd < self.f.min_liquidity_usd:
            res.motivi_scarto.append(f"liquidità ${c.liquidity_usd:,.0f} < ${self.f.min_liquidity_usd:,.0f}")
        if c.volume_5m_usd < self.f.min_volume_5m_usd:
            res.motivi_scarto.append(f"volume 5m ${c.volume_5m_usd:,.0f} troppo basso")
        if c.eta_minuti > self.f.max_eta_pool_minuti:
            res.motivi_scarto.append(f"pool troppo vecchio ({c.eta_minuti:.0f} min)")
        if c.eta_minuti < self.f.min_eta_pool_minuti:
            res.motivi_scarto.append(f"pool troppo giovane ({c.eta_minuti:.0f} min)")
        if c.buy_sell_ratio < 0.8:
            res.motivi_scarto.append(f"pressione di vendita alta (buy/sell {c.buy_sell_ratio:.2f})")

        # Entry band di market cap (sweet spot 150k–1.5M)
        if c.market_cap_usd < self.f.min_market_cap_usd:
            res.motivi_scarto.append(f"mcap ${c.market_cap_usd:,.0f} sotto la entry band (fase lancio/sniper)")
        if c.market_cap_usd > self.f.max_market_cap_usd:
            res.motivi_scarto.append(f"mcap ${c.market_cap_usd:,.0f} sopra la entry band (multiplo già fatto)")
        if c.market_cap_usd > 0 and c.liquidity_usd / c.market_cap_usd < self.f.min_liq_su_mcap:
            res.motivi_scarto.append(f"liq/mcap {c.liquidity_usd / c.market_cap_usd:.1%} < {self.f.min_liq_su_mcap:.0%} (mcap gonfiato)")

        # Se già bocciato sui quantitativi, evita la chiamata a RugCheck
        if res.motivi_scarto:
            res.passed = False
            return res

        # ---- 2. Controlli on-chain via RugCheck ----
        report = await self._rugcheck_report(c.mint)
        if report is None:
            res.motivi_scarto.append("RugCheck non disponibile → scarto prudenziale")
            res.passed = False
            return res

        res.rugcheck_score = report.get("score_normalised") or report.get("score")

        token_meta = report.get("token") or {}
        mint_auth = token_meta.get("mintAuthority")
        freeze_auth = token_meta.get("freezeAuthority")
        res.mint_revocato = mint_auth is None
        res.freeze_revocato = freeze_auth is None

        if self.f.richiedi_mint_revocato and not res.mint_revocato:
            res.motivi_scarto.append("mint authority NON revocata (possono stampare token)")
        if self.f.richiedi_freeze_revocato and not res.freeze_revocato:
            res.motivi_scarto.append("freeze authority NON revocata (possono congelare i wallet)")

        # LP lock/burn
        markets = report.get("markets") or []
        lp_ok = any(
            (m.get("lp") or {}).get("lpLockedPct", 0) >= 90
            for m in markets
        )
        res.lp_bloccata = lp_ok
        if self.f.richiedi_lp_bloccata and not lp_ok:
            res.motivi_scarto.append("LP non bloccata/bruciata (rug pull possibile)")

        # Concentrazione holder
        top_holders = report.get("topHolders") or []
        top10 = sum(h.get("pct", 0) for h in top_holders[:10]) / 100.0
        res.top10_holder_pct = top10
        if top10 > self.f.max_holder_top10_pct:
            res.motivi_scarto.append(f"top10 holder detengono {top10:.0%} > {self.f.max_holder_top10_pct:.0%}")

        # Score complessivo
        if res.rugcheck_score is not None and res.rugcheck_score < self.f.min_rugcheck_score:
            res.motivi_scarto.append(f"RugCheck score {res.rugcheck_score} sotto soglia")

        res.passed = not res.motivi_scarto
        if res.passed:
            log.info("✅ %s ha passato tutti i filtri", c.symbol)
        else:
            log.debug("❌ %s scartato: %s", c.symbol, "; ".join(res.motivi_scarto))
        return res

    async def _rugcheck_report(self, mint: str) -> dict | None:
        url = f"{CONFIG.api.rugcheck_url}/tokens/{mint}/report"
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    return await r.json()
                log.warning("RugCheck HTTP %s per %s", r.status, mint)
        except Exception as e:
            log.error("Errore RugCheck: %s", e)
        return None
