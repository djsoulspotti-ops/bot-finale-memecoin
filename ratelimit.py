"""
ratelimit.py — Limitatore di frequenza per fornitore, con backoff sui 429.

PERCHÉ ESISTE QUESTO FILE
-------------------------
Alzando le cadenze per l'alta frequenza si sfonda il rate limit del piano
gratuito, e il modo in cui si manifesta è insidioso: Jupiter risponde HTTP 429,
il codice riceve una lista vuota e il bot continua a girare come se il mercato
non avesse candidati. Nessun errore, nessun crash — solo un bot che non compra
mai. È la stessa classe di guasto silenzioso che teneva fermo il bot prima.

Misurato in questo repo: con discovery ogni 4s su 3 feed (45 richieste/minuto)
più il monitoraggio prezzi ogni 1.5s (40/minuto) si arriva a ~85 richieste al
minuto contro un tetto di 60, e dopo circa 90 secondi TUTTI i feed
restituiscono 0 elementi.

Il limite del piano gratuito Jupiter è 60 richieste al minuto su finestra di
60 secondi. Il carico va quindi diviso su fornitori diversi (Jupiter per la
discovery, DexScreener per il monitoraggio dei prezzi: budget separati) e
contenuto sotto il tetto con un token bucket.

`consuma()` non solleva eccezioni: attende il tempo necessario. `segnala_429()`
apre una finestra di backoff durante la quale tutte le richieste a quel
fornitore vengono rallentate, così un limite superato si risolve da sé invece
di trasformarsi in un blocco permanente.
"""

import asyncio
import logging
import time

log = logging.getLogger("ratelimit")


class Limitatore:
    """Token bucket asincrono con backoff esponenziale sui 429."""

    def __init__(self, nome: str, al_minuto: int, burst: int | None = None):
        self.nome = nome
        self.al_minuto = max(1, al_minuto)
        self.capacita = burst if burst is not None else max(2, self.al_minuto // 6)
        self.gettoni = float(self.capacita)
        self.ultimo = time.monotonic()
        self._lock = asyncio.Lock()

        self._backoff_fino = 0.0
        self._backoff_sec = 0.0
        self.richieste = 0
        self.attese = 0
        self.errori_429 = 0

    @property
    def al_secondo(self) -> float:
        return self.al_minuto / 60.0

    async def consuma(self, quanti: int = 1):
        """Attende fino a poter effettuare `quanti` richieste."""
        while True:
            async with self._lock:
                ora = time.monotonic()

                if ora < self._backoff_fino:
                    attesa = self._backoff_fino - ora
                else:
                    self.gettoni = min(
                        float(self.capacita),
                        self.gettoni + (ora - self.ultimo) * self.al_secondo,
                    )
                    self.ultimo = ora
                    if self.gettoni >= quanti:
                        self.gettoni -= quanti
                        self.richieste += quanti
                        return
                    attesa = (quanti - self.gettoni) / self.al_secondo

            self.attese += 1
            await asyncio.sleep(min(attesa, 5.0))

    def segnala_429(self):
        """Da chiamare quando il fornitore risponde 429. Raddoppia la finestra
        di backoff fino a 60 secondi."""
        self.errori_429 += 1
        self._backoff_sec = min(60.0, max(2.0, self._backoff_sec * 2))
        self._backoff_fino = time.monotonic() + self._backoff_sec
        self.gettoni = 0.0
        log.warning(
            "⚠️ %s: rate limit superato (429 numero %d). Pausa di %.0fs su questo "
            "fornitore. Se accade spesso, alza gli intervalli in config.py oppure "
            "usa una API key (JUPITER_BASE_URL=https://api.jup.ag nel .env).",
            self.nome, self.errori_429, self._backoff_sec)

    def segnala_ok(self):
        """Una risposta valida rilassa gradualmente il backoff accumulato."""
        if self._backoff_sec and time.monotonic() > self._backoff_fino:
            self._backoff_sec = max(0.0, self._backoff_sec / 2)

    @property
    def in_backoff(self) -> bool:
        return time.monotonic() < self._backoff_fino

    def statistiche(self) -> dict:
        return {"fornitore": self.nome, "al_minuto": self.al_minuto,
                "richieste": self.richieste, "attese": self.attese,
                "errori_429": self.errori_429, "in_backoff": self.in_backoff}


class _Registro:
    """Un limitatore per fornitore, condiviso da tutti i moduli."""

    def __init__(self):
        self._limitatori: dict[str, Limitatore] = {}

    def registra(self, nome: str, al_minuto: int) -> Limitatore:
        lim = self._limitatori.get(nome)
        if lim is None:
            lim = Limitatore(nome, al_minuto)
            self._limitatori[nome] = lim
        else:
            lim.al_minuto = max(1, al_minuto)
        return lim

    def get(self, nome: str) -> Limitatore:
        return self._limitatori.get(nome) or self.registra(nome, 60)

    def tutte_le_statistiche(self) -> list[dict]:
        return [l.statistiche() for l in self._limitatori.values()]


REGISTRO = _Registro()


async def richiesta_json(session, lim: Limitatore, metodo: str, url: str, **kw):
    """Wrapper unico per le richieste HTTP soggette a rate limit.

    Ritorna (dati, errore). `dati` è None se la richiesta non è andata a buon
    fine; `errore` è una stringa leggibile. Un 429 viene segnalato al
    limitatore invece di essere confuso con "nessun risultato": è la
    distinzione che mancava e che rendeva il guasto invisibile.
    """
    await lim.consuma()
    try:
        async with session.request(metodo, url, **kw) as r:
            if r.status == 429:
                lim.segnala_429()
                return None, "429 rate limit"
            if r.status != 200:
                return None, f"HTTP {r.status}"
            lim.segnala_ok()
            return await r.json(), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
