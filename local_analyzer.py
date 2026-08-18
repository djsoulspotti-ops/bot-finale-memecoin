"""
local_analyzer.py — Score quantitativo 0-100 del token, 100% locale.

Nessuna chiamata di rete, nessun costo, nessuna AI: solo una formula
deterministica sui dati che lo scanner ha già raccolto. È il gate finale
prima dell'acquisto.

COSA È CAMBIATO RISPETTO ALLA VERSIONE PRECEDENTE
-------------------------------------------------
Il vecchio scoring assegnava 25 dei 100 punti con
`punti += 25 * min(1, rugcheck_score / 1000)`. Poiché `score_normalised` di
RugCheck è un punteggio di RISCHIO su scala 0-100, quella formula:
  - premiava i token PIÙ rischiosi (più alto il rischio, più punti);
  - con un token sano (score 1) assegnava 0,025 punti su 25 disponibili.
Il massimo score realmente ottenibile crollava così a ~75 su una soglia di
70: anche un token perfetto su tutto il resto passava per un soffio.

I 100 punti sono ora distribuiti su cinque componenti che misurano cose
diverse tra loro, tutte disponibili senza costo aggiuntivo:

  25  qualità del flusso   — volume organico vs volume totale (anti wash trading)
  25  distribuzione        — top holder, quota del dev, lanci seriali del dev
  20  pressione d'acquisto — buy/sell, compratori netti, numero di trader
  15  struttura            — liquidità su market cap, variazione della liquidità
  15  trazione             — crescita degli holder e conferma su più feed
"""

import logging

from config import CONFIG
from filters import FilterResult
from scanner import TokenCandidate

log = logging.getLogger("local_analyzer")


def _scala(valore: float, minimo: float, massimo: float) -> float:
    """Normalizza `valore` in 0..1 dentro [minimo, massimo]."""
    if massimo <= minimo:
        return 0.0
    return max(0.0, min(1.0, (valore - minimo) / (massimo - minimo)))


def _qualita_flusso(c: TokenCandidate) -> tuple[float, str | None]:
    """25 punti. Quanto del volume è reale invece di wash trading."""
    # organicScore esiste solo dopo ~30 minuti di vita (misurato: 0 per il
    # 100% dei token più giovani). Sotto quella soglia si usa un proxy
    # strutturale: numero di trader distinti rispetto al numero di scambi.
    # Molti scambi ma pochissimi trader = uno o due wallet che si passano il
    # token, che è esattamente la firma del wash trading.
    if c.eta_minuti >= CONFIG.filters.organic_gate_da_eta_minuti and c.organic_score > 0:
        punti = 25.0 * _scala(c.organic_score, 0, 70)
        rischio = "volume prevalentemente non organico" if c.organic_score < 25 else None
        return punti, rischio

    scambi = c.buys_5m + c.sells_5m
    if scambi < 5:
        return 12.0, None  # troppo presto per giudicare: punteggio neutro
    diversita = c.traders_5m / scambi
    punti = 25.0 * _scala(diversita, 0.05, 0.45)
    rischio = (f"solo {c.traders_5m} trader distinti su {scambi} scambi (possibile wash trading)"
               if diversita < 0.12 else None)
    return punti, rischio


def _distribuzione(c: TokenCandidate) -> tuple[float, list[str]]:
    """25 punti. Chi tiene il token e chi lo ha creato."""
    punti, rischi = 0.0, []

    # Top holder: 12 punti. Misurato mediana 23.5%, p75 36.8%.
    if c.top_holders_pct is None:
        punti += 6.0  # dato assente: metà punteggio, non un premio
    else:
        punti += 12.0 * (1.0 - _scala(c.top_holders_pct, 12.0, 55.0))
        if c.top_holders_pct > 45:
            rischi.append(f"top holder al {c.top_holders_pct:.0f}%")

    # Quota del dev: 6 punti.
    if c.dev_balance_pct is None:
        punti += 3.0
    else:
        punti += 6.0 * (1.0 - _scala(c.dev_balance_pct, 1.0, 15.0))
        if c.dev_balance_pct > 10:
            rischi.append(f"il dev detiene il {c.dev_balance_pct:.0f}% della supply")

    # Storia del dev: 7 punti. Misurato mediana 1, p75 61, p90 3897.
    # Un dev al primo lancio non è garanzia di niente, ma un dev al
    # duemillesimo è una fabbrica di token.
    if c.dev_mints is None:
        punti += 3.5
    else:
        punti += 7.0 * (1.0 - _scala(c.dev_mints, 2, 40))
        if c.dev_mints > 20:
            rischi.append(f"il dev ha già lanciato {c.dev_mints} token")

    return punti, rischi


def _pressione_acquisto(c: TokenCandidate) -> tuple[float, str | None]:
    """20 punti. Il flusso sta comprando o distribuendo."""
    punti = 12.0 * _scala(c.buy_sell_ratio, 0.8, 2.0)
    # numNetBuyers = compratori netti: più affidabile del solo buy/sell ratio,
    # perché conta wallet distinti invece di transazioni ripetibili.
    punti += 5.0 * _scala(float(c.net_buyers_5m), 0.0, 25.0)
    punti += 3.0 * _scala(float(c.traders_5m), 5.0, 60.0)
    rischio = ("pressione di vendita superiore agli acquisti"
               if c.buy_sell_ratio < 0.95 else None)
    return punti, rischio


def _struttura(c: TokenCandidate) -> tuple[float, str | None]:
    """15 punti. Il rapporto tra liquidità e valutazione, e come si sta muovendo."""
    # liq/mcap: 10 punti. Sopra il 12% è sano, sotto il 3% è una trappola.
    punti = 10.0 * _scala(c.liq_su_mcap, 0.03, 0.12)
    # Liquidità in aumento: 5 punti. Liquidità che scende mentre il prezzo
    # sale è il preludio classico al rug.
    punti += 5.0 * _scala(c.liquidity_change_5m, -5.0, 15.0)
    rischio = None
    if c.liquidity_change_5m < -10:
        rischio = f"liquidità in calo del {abs(c.liquidity_change_5m):.0f}% in 5m"
    elif c.liq_su_mcap < 0.05:
        rischio = f"liquidità solo il {c.liq_su_mcap:.1%} del mcap"
    return punti, rischio


def _trazione(c: TokenCandidate) -> tuple[float, str | None]:
    """15 punti. Il token sta acquisendo partecipanti reali."""
    punti = 0.0
    # Holder in crescita: 8 punti
    if c.holder_change_1h is None:
        punti += 4.0
    else:
        punti += 8.0 * _scala(c.holder_change_1h, 0.0, 20.0)
    # Numero assoluto di holder: 4 punti
    if c.holder_count is not None:
        punti += 4.0 * _scala(float(c.holder_count), 30.0, 800.0)
    # Presenza su più feed contemporaneamente: 3 punti. Comparire insieme in
    # "recent", "trending" e "traded" significa nuovo E in movimento E con
    # volume: è una conferma incrociata gratuita.
    punti += min(3.0, (len(set(c.feed)) - 1) * 1.5)
    rischio = ("holder in calo" if (c.holder_change_1h or 0) < -5 else None)
    return punti, rischio


class LocalAnalyzer:
    """Interfaccia stabile: analizza(candidato, risultato_filtri) -> dict."""

    async def analizza(self, c: TokenCandidate, f: FilterResult | None = None) -> dict:
        rischi: list[str] = []

        p_flusso, r_flusso = _qualita_flusso(c)
        p_distr, r_distr = _distribuzione(c)
        p_press, r_press = _pressione_acquisto(c)
        p_strut, r_strut = _struttura(c)
        p_traz, r_traz = _trazione(c)

        for r in (r_flusso, r_press, r_strut, r_traz):
            if r:
                rischi.append(r)
        rischi.extend(r_distr)

        score = int(round(max(0.0, min(100.0, p_flusso + p_distr + p_press + p_strut + p_traz))))
        soglia = CONFIG.min_score_locale
        decisione = "COMPRA" if score >= soglia else "SCARTA"

        dettaglio = (f"flusso {p_flusso:.0f}/25 · distribuzione {p_distr:.0f}/25 · "
                     f"pressione {p_press:.0f}/20 · struttura {p_strut:.0f}/15 · "
                     f"trazione {p_traz:.0f}/15")
        log.debug("📐 %s → score=%d (%s) | %s", c.symbol, score, decisione, dettaglio)
        return {
            "score": score,
            "decisione": decisione,
            "motivazione": dettaglio,
            "rischi": rischi,
            "componenti": {
                "flusso": round(p_flusso, 1), "distribuzione": round(p_distr, 1),
                "pressione": round(p_press, 1), "struttura": round(p_strut, 1),
                "trazione": round(p_traz, 1),
            },
        }
