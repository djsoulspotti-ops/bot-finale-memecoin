"""
metrics.py — Contatori dell'imbuto degli scarti.

È lo strumento che rende il bot tarabile. Senza sapere QUALE stadio uccide i
candidati, ogni modifica alle soglie è un tentativo alla cieca: si allarga il
parametro sbagliato, non cambia niente, e non si capisce perché.

La versione precedente del bot non aveva niente di simile: un candidato
scartato produceva al massimo una riga di log.debug, e per capire dove
morivano i candidati bisognava rileggere migliaia di righe a mano.

Ogni `log_imbuto_ogni_sec` secondi viene stampato un riepilogo come:

    IMBUTO (ultimi 120s) — 1880 valutati, 3 promossi (0.2%)
        842  eta: pool troppo vecchio
        410  audit: mint authority non revocata
        295  audit: dev con lanci seriali
        188  metriche: liquidità sotto soglia
         94  momentum sotto soglia
         48  sentiment sotto soglia

Il file `imbuto.jsonl` conserva gli stessi dati nel tempo, così si può vedere
se una modifica alle soglie ha spostato davvero il collo di bottiglia.
"""

import json
import logging
import time
from collections import Counter

from config import CONFIG
from ratelimit import REGISTRO

log = logging.getLogger("metrics")

# Ordine canonico degli stadi: è l'ordine in cui il bot li applica, quindi
# leggendo dall'alto si trova subito il primo collo di bottiglia.
STADI = (
    "audit_onchain",
    "eta",
    "metriche",
    "momentum",
    "sentiment",
    "score_locale",
    "deep_check",
    "rivalidazione",
    "esecuzione",
)


class Imbuto:
    def __init__(self):
        self.motivi: Counter = Counter()
        self.per_stadio: Counter = Counter()
        self.valutati = 0
        self.promossi = 0
        self.acquisti_ok = 0
        self.acquisti_ko = 0
        self.inizio = time.time()
        self._ultimo_log = time.time()
        # Totali cumulativi dall'avvio, non azzerati dal report periodico
        self.totale_valutati = 0
        self.totale_promossi = 0

    def valutato(self, n: int = 1):
        self.valutati += n
        self.totale_valutati += n

    def scarto(self, stadio: str, motivo: str):
        """Registra uno scarto. `motivo` viene normalizzato togliendo i numeri
        specifici, altrimenti ogni token produrrebbe una voce diversa
        ('liquidità $4.636' vs '$5.301') e il conteggio sarebbe inutile."""
        self.per_stadio[stadio] += 1
        self.motivi[f"{stadio}: {self._normalizza(motivo)}"] += 1

    def promosso(self):
        self.promossi += 1
        self.totale_promossi += 1

    def acquisto(self, ok: bool):
        if ok:
            self.acquisti_ok += 1
        else:
            self.acquisti_ko += 1

    # Etichette stabili per i motivi ricorrenti. Aggregare per sottostringa
    # invece che per testo esatto è indispensabile: il motivo contiene sempre
    # valori specifici del token ("liquidità $4.636" vs "$5.301") e senza
    # questa mappa ogni singolo token produrrebbe una voce da 1 nel conteggio,
    # rendendo il report inutile proprio nel momento in cui serve.
    ETICHETTE = (
        ("dev ha già lanciato", "dev con lanci seriali"),
        ("dev detiene", "dev detiene troppa supply"),
        ("mint authority", "mint authority non revocata"),
        ("freeze authority", "freeze authority non revocata"),
        ("impersonificazione", "simbolo di un asset noto"),
        ("troppo vecchio", "pool troppo vecchio"),
        ("troppo giovane", "pool troppo giovane"),
        ("liquidità", "liquidità sotto soglia"),
        ("volume 5m", "volume sotto soglia"),
        ("liq/mcap", "liquidità bassa sul mcap"),
        ("sotto la banda", "mcap sotto la banda"),
        ("sopra la banda", "mcap sopra la banda"),
        ("tetto del pump", "già pumpato: rischio tetto"),
        ("top holder", "top holder troppo concentrati"),
        ("organic score", "volume poco organico"),
        ("volume 1h è organico", "volume poco organico"),
        ("pressione di vendita", "pressione di vendita alta"),
        ("momentum", "momentum sotto soglia"),
        ("presenza social", "presenza social sotto soglia"),
        ("score locale", "score locale sotto soglia"),
        ("RugCheck rischio", "RugCheck: rischio alto"),
        ("RUGGED", "RugCheck: già ruggato"),
        ("pumpato dopo", "pumpato tra scoring e acquisto"),
        ("segnale decaduto", "segnale decaduto prima dell'acquisto"),
    )

    @classmethod
    def _normalizza(cls, motivo: str) -> str:
        """Riduce un motivo specifico del token a un'etichetta aggregabile."""
        for frammento, etichetta in cls.ETICHETTE:
            if frammento in motivo:
                return etichetta
        # Nessuna etichetta nota: taglia al primo numero o importo, così
        # almeno resta leggibile invece di frantumarsi in mille voci.
        parole = []
        for parola in motivo.split():
            if any(ch.isdigit() for ch in parola) or parola.startswith("$"):
                break
            parole.append(parola)
        return " ".join(parole).strip(" :·,-(") or motivo[:40]

    # ---------------- REPORT ----------------

    def forse_logga(self):
        ogni = CONFIG.log_imbuto_ogni_sec
        if ogni <= 0 or (time.time() - self._ultimo_log) < ogni:
            return
        self.logga()

    def logga(self):
        finestra = max(1.0, time.time() - self._ultimo_log)
        if self.valutati == 0:
            log.info("IMBUTO (ultimi %.0fs) — nessun candidato valutato", finestra)
            self._reset_finestra()
            return

        quota = self.promossi / self.valutati * 100
        righe = [f"IMBUTO (ultimi {finestra:.0f}s) — {self.valutati} valutati, "
                 f"{self.promossi} promossi ({quota:.1f}%)"]
        for motivo, n in self.motivi.most_common(10):
            righe.append(f"    {n:5d}  {motivo}")
        if self.acquisti_ok or self.acquisti_ko:
            righe.append(f"    acquisti: {self.acquisti_ok} riusciti, {self.acquisti_ko} falliti")

        # Stato dei rate limiter. Se un fornitore accumula 429 il bot smette di
        # vedere candidati senza che nulla sembri rotto: va scritto qui accanto
        # ai numeri dell'imbuto, altrimenti si finisce ad allargare le soglie
        # per un problema che non c'entra niente con le soglie.
        for st in REGISTRO.tutte_le_statistiche():
            if st["errori_429"] or st["in_backoff"]:
                righe.append(
                    f"    ⚠️ {st['fornitore']}: {st['errori_429']} risposte 429, "
                    f"{st['richieste']} richieste totali"
                    f"{' — IN BACKOFF ORA' if st['in_backoff'] else ''}")
        log.info("\n".join(righe))

        try:
            with open("imbuto.jsonl", "a") as f:
                f.write(json.dumps({
                    "ts": time.time(),
                    "finestra_sec": round(finestra, 1),
                    "valutati": self.valutati,
                    "promossi": self.promossi,
                    "acquisti_ok": self.acquisti_ok,
                    "acquisti_ko": self.acquisti_ko,
                    "per_stadio": dict(self.per_stadio),
                    "motivi": dict(self.motivi.most_common(25)),
                    "rate_limit": REGISTRO.tutte_le_statistiche(),
                }) + "\n")
        except OSError as e:
            log.debug("Impossibile scrivere imbuto.jsonl: %s", e)

        self._reset_finestra()

    def _reset_finestra(self):
        self.motivi.clear()
        self.per_stadio.clear()
        self.valutati = 0
        self.promossi = 0
        self.acquisti_ok = 0
        self.acquisti_ko = 0
        self._ultimo_log = time.time()

    def riepilogo_breve(self) -> str:
        """Una riga per le notifiche Telegram e la dashboard."""
        ore = (time.time() - self.inizio) / 3600
        return (f"{self.totale_valutati} candidati valutati in {ore:.1f}h, "
                f"{self.totale_promossi} promossi")


# Istanza condivisa dal bot
IMBUTO = Imbuto()
