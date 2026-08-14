"""
local_analyzer.py — Analisi quantitativa del token, 100% locale.

Sostituisce claude_analyzer.py: stessi criteri che venivano descritti
all'AI nel prompt originale, applicati come formula deterministica invece
che come giudizio LLM. Zero chiamate di rete, zero costo, zero dipendenza
da ANTHROPIC_API_KEY.

Criteri (stesso ordine del prompt originale, punteggio su 100):
  1. Rapporto volume/liquidità 5m — sano 0.2-2.0        (peso 20)
  2. Rapporto buy/sell — sano > 1.2                      (peso 20)
  3. Impersonificazione/simbolo sospetto (blacklist)     (penalità secca)
  4. Market cap / liquidità — sospetto se > 20            (peso 20)
  5. RugCheck score (0-5000) + distribuzione holder      (peso 40: 25+15)

Non replica la lettura "qualitativa" che un LLM può fare su notizie o
contesto recente — quella resta un limite noto e dichiarato, non finto.
"""

import logging

from filters import FilterResult
from scanner import TokenCandidate

log = logging.getLogger("local_analyzer")

# Simboli di progetti noti/major: un token nuovo che li copia è quasi
# sempre un tentativo di impersonificazione, non il progetto vero.
SIMBOLI_PROTETTI = {"SOL", "BTC", "ETH", "USDC", "USDT", "BONK", "WIF", "JUP", "RAY"}


def _punteggio_volume_liquidita(c: TokenCandidate) -> tuple[float, str | None]:
    rapporto = c.volume_5m_usd / max(c.liquidity_usd, 1)
    if 0.2 <= rapporto <= 2.0:
        return 20.0, None
    if rapporto < 0.2:
        return 20.0 * (rapporto / 0.2), "volume basso rispetto alla liquidità"
    # oltre 2.0 il punteggio decresce, ma non azzera subito: può essere solo hype forte
    punti = max(0.0, 20.0 - (rapporto - 2.0) * 4.0)
    rischio = "volume anomalo rispetto alla liquidità (possibile wash trading)" if punti < 10 else None
    return punti, rischio


def _punteggio_buy_sell(c: TokenCandidate) -> tuple[float, str | None]:
    r = c.buy_sell_ratio
    if r >= 1.2:
        return min(20.0, 10.0 + (r - 1.2) * 10.0), None
    punti = 20.0 * (r / 1.2)
    rischio = "pressione di vendita superiore agli acquisti" if r < 0.9 else None
    return punti, rischio


def _penalita_impersonificazione(c: TokenCandidate) -> str | None:
    simbolo = (c.symbol or "").strip().upper()
    if simbolo in SIMBOLI_PROTETTI:
        return f"simbolo ${simbolo} coincide con un asset noto — possibile impersonificazione"
    return None


def _punteggio_mcap_liquidita(c: TokenCandidate) -> tuple[float, str | None]:
    rapporto = c.market_cap_usd / max(c.liquidity_usd, 1)
    if rapporto <= 8:
        return 20.0, None
    if rapporto <= 20:
        return 20.0 * (1 - (rapporto - 8) / 12), None
    return 0.0, f"mcap/liquidità {rapporto:.0f}x molto alto (possibile mcap gonfiato)"


def _punteggio_rugcheck_holder(f: FilterResult) -> tuple[float, list[str]]:
    punti = 0.0
    rischi = []
    if f.rugcheck_score is not None:
        punti += 25.0 * min(1.0, f.rugcheck_score / 1000)
    else:
        rischi.append("RugCheck score non disponibile")
    if f.top10_holder_pct is not None:
        if f.top10_holder_pct <= 0.15:
            punti += 15.0
        elif f.top10_holder_pct <= 0.35:
            punti += 15.0 * (1 - (f.top10_holder_pct - 0.15) / 0.20)
        else:
            rischi.append(f"top10 holder al {f.top10_holder_pct:.0%}")
    return punti, rischi


class LocalAnalyzer:
    """Interfaccia identica a ClaudeAnalyzer: analizza(c, f) -> dict.
    Nessun costruttore con sessione HTTP: non serve, non fa chiamate di rete."""

    async def analizza(self, c: TokenCandidate, f: FilterResult) -> dict:
        rischi: list[str] = []

        p_vol, r_vol = _punteggio_volume_liquidita(c)
        p_bs, r_bs = _punteggio_buy_sell(c)
        p_mcap, r_mcap = _punteggio_mcap_liquidita(c)
        p_rug, r_rug = _punteggio_rugcheck_holder(f)

        for r in (r_vol, r_bs, r_mcap):
            if r:
                rischi.append(r)
        rischi.extend(r_rug)

        impersonificazione = _penalita_impersonificazione(c)
        if impersonificazione:
            rischi.append(impersonificazione)

        score = round(p_vol + p_bs + p_mcap + p_rug)
        score = max(0, min(100, score))
        # Impersonificazione è uno scarto secco, non solo un malus sul punteggio.
        if impersonificazione:
            score = min(score, 20)

        decisione = "COMPRA" if score >= 70 else "SCARTA"
        motivazione = (
            f"vol/liq={c.volume_5m_usd / max(c.liquidity_usd, 1):.2f} "
            f"buy/sell={c.buy_sell_ratio:.2f} mcap/liq={c.market_cap_usd / max(c.liquidity_usd, 1):.1f} "
            f"rugcheck={f.rugcheck_score} top10={f.top10_holder_pct}"
        )
        log.info("📐 Analisi locale %s → score=%d (%s)", c.symbol, score, decisione)
        return {"score": score, "decisione": decisione, "motivazione": motivazione, "rischi": rischi}
