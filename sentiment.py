"""
sentiment.py — Presenza social del token, 100% locale e senza chiamate di rete.

Nota di onestà tecnica, invariata rispetto a prima: le API ufficiali di
X/Twitter costano da ~200 $/mese e Telegram/Discord non espongono ricerche
pubbliche. Senza una ricerca web reale NON si distingue hype organico da
shill coordinato, e non ho intenzione di far finta del contrario inventando
un segnale che non esiste.

Quello che si può misurare a costo zero è la presenza STRUTTURATA: un
progetto che investe nella propria presenza compila i campi, un rug parte da
un template vuoto. È un gate debole ma reale, e viene usato come tale.

Cosa cambia: i segnali arrivano ora dai campi che Jupiter restituisce già nel
feed dello scanner (`twitter`, `telegram`, `website`, `isVerified`, `icon`)
invece dal blocco `info` di DexScreener, che richiedeva una richiesta a parte.
Zero I/O in questo modulo.
"""

import logging

import aiohttp

from scanner import TokenCandidate

log = logging.getLogger("sentiment")


class SentimentAnalyzer:
    def __init__(self, session: aiohttp.ClientSession | None = None):
        # La sessione non serve (nessuna rete), ma il parametro resta per non
        # rompere la firma usata da main.py.
        self.session = session

    @staticmethod
    def segnali_strutturali(c: TokenCandidate) -> dict:
        punti = 0
        punti += 30 if c.ha_twitter else 0
        punti += 22 if c.ha_telegram else 0
        punti += 22 if c.ha_sito else 0
        punti += 11 if c.ha_icona else 0
        # isVerified su Jupiter richiede una verifica manuale: è il segnale
        # più forte del gruppo, ma quasi nessuna memecoin fresca lo ha.
        punti += 15 if c.verificato else 0
        return {
            "score_strutturale": punti,
            "ha_twitter": c.ha_twitter,
            "ha_telegram": c.ha_telegram,
            "ha_sito": c.ha_sito,
            "verificato": c.verificato,
        }

    async def analizza(self, c: TokenCandidate) -> dict:
        """Async solo per compatibilità: main.py fa gather su questa insieme
        ad altre valutazioni."""
        s = self.segnali_strutturali(c)
        punti = s["score_strutturale"]

        if punti >= 60:
            hype_type, note = "buona_presenza", "presenza social strutturata solida"
        elif punti >= 25:
            hype_type, note = "presenza_parziale", "presenza social strutturata parziale"
        else:
            hype_type, note = "assente", "nessuna presenza social strutturata"

        log.debug("📣 %s → sentiment %d (%s)", c.symbol, punti, hype_type)
        return {"sentiment_score": punti, "hype_type": hype_type, "note": note, **s}
