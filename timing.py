"""
timing.py — Momentum score d'ingresso (0-100).

Non basta "il token è buono": deve stare accelerando ADESSO.
Entrare su un token di qualità ma in fase di distribuzione = comprare il dump.

Componenti:
  40% accelerazione prezzo   → variazione 5m positiva e significativa
  30% concentrazione volume  → volume 5m alto rispetto al volume 1h
                               (volume fresco = interesse ora, non un'ora fa)
  30% pressione in acquisto  → buy/sell ratio > 1 e in salita
"""

import logging

from scanner import TokenCandidate

log = logging.getLogger("timing")


def momentum_score(c: TokenCandidate) -> float:
    # 1. Accelerazione prezzo: +20% in 5m = massimo punteggio, negativo = 0
    price_comp = max(0.0, min(c.price_change_5m / 20.0, 1.0))

    # 2. Concentrazione volume: se il volume 5m è >= 1/6 del volume 1h,
    #    l'attività è concentrata adesso (5m = 1/12 di un'ora: 1/6 = 2x la media)
    vol_1h = float((c.raw.get("volume") or {}).get("h1") or 0)
    if vol_1h <= 0:
        vol_comp = 0.5  # pool giovanissimo: neutro
    else:
        vol_comp = min((c.volume_5m_usd / vol_1h) * 6.0, 1.0)

    # 3. Pressione in acquisto: ratio 2.0+ = massimo
    buy_comp = min(c.buy_sell_ratio / 2.0, 1.0)

    score = 100.0 * (0.40 * price_comp + 0.30 * vol_comp + 0.30 * buy_comp)
    log.debug("Momentum %s = %.0f (px %.2f, vol %.2f, buy %.2f)",
              c.symbol, score, price_comp, vol_comp, buy_comp)
    return round(score, 1)


def score_composito(claude_score: float, sentiment_score: float, momentum: float) -> float:
    """Score unico 0-100 usato per il Kelly sizing e il ranking nel cluster."""
    return round(0.50 * claude_score + 0.30 * sentiment_score + 0.20 * momentum, 1)
