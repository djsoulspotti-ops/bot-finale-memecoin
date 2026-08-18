"""
timing.py — Momentum score d'ingresso (0-100).

Non basta "il token è buono": deve stare accelerando ADESSO. Entrare su un
token di qualità in fase di distribuzione significa comprare il dump.

Componenti (tutte da `stats5m` di Jupiter, nessuna chiamata di rete):
  35%  accelerazione prezzo   → variazione 5m, con PENALITÀ sopra il tetto:
                                un +1500% in 5 minuti non è un segnale
                                d'ingresso, è il tetto di un pump
  25%  accelerazione volume   → volumeChange: il volume di ADESSO rispetto
                                alla finestra precedente
  25%  pressione in acquisto  → buy/sell ratio e compratori netti
  15%  partecipazione         → trader distinti: un movimento con 3 wallet
                                non è un movimento

Il momentum era il primo killer di candidati della versione precedente (18
scarti su 22 nel dry-run), in gran parte perché la componente volume usava un
rapporto tra volume 5m e volume 1h ricavato da DexScreener, che per un pool
giovane è quasi sempre degenere. Jupiter espone `volumeChange` direttamente.
"""

import logging

from config import CONFIG
from scanner import TokenCandidate

log = logging.getLogger("timing")


def _scala(v: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def momentum_score(c: TokenCandidate) -> float:
    tetto = CONFIG.filters.max_price_change_5m_ingresso

    # 1. Accelerazione prezzo. Sale fino a +25% in 5m, poi resta piena fino al
    #    tetto, e OLTRE il tetto decade: comprare a +1500% è comprare il picco.
    if c.price_change_5m <= 0:
        px = 0.0
    elif c.price_change_5m <= 25.0:
        px = _scala(c.price_change_5m, 0.0, 25.0)
    elif c.price_change_5m <= tetto:
        px = 1.0
    else:
        px = max(0.0, 1.0 - _scala(c.price_change_5m, tetto, tetto * 4))

    # 2. Accelerazione volume: +100% sulla finestra precedente = pieno.
    vol = _scala(c.volume_change_5m, 0.0, 100.0)

    # 3. Pressione in acquisto: ratio 2.0 = pieno, più i compratori netti.
    buy = 0.65 * _scala(c.buy_sell_ratio, 0.8, 2.0) + 0.35 * _scala(float(c.net_buyers_5m), 0.0, 20.0)

    # 4. Partecipazione: 40 trader distinti in 5m = pieno.
    part = _scala(float(c.traders_5m), 4.0, 40.0)

    score = 100.0 * (0.35 * px + 0.25 * vol + 0.25 * buy + 0.15 * part)
    log.debug("Momentum %s = %.0f (px %.2f, vol %.2f, buy %.2f, part %.2f)",
              c.symbol, score, px, vol, buy, part)
    return round(score, 1)


def score_composito(score_locale: float, sentiment_score: float, momentum: float) -> float:
    """Score unico 0-100 per il Kelly sizing e il ranking nel cluster.

    Lo score locale pesa più di prima (55% invece di 50%) perché ora misura
    cinque dimensioni indipendenti su dati on-chain reali, mentre il sentiment
    resta un proxy strutturale dichiaratamente debole e scende al 20%.
    """
    return round(0.55 * score_locale + 0.20 * sentiment_score + 0.25 * momentum, 1)
