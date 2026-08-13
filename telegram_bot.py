"""
telegram_bot.py — Notifiche e comandi via Telegram.

Il bot manda un messaggio ad ogni evento importante (apertura/chiusura
posizione, stop di sicurezza, circuit breaker, cambio di comando) e ascolta
la chat per i comandi PARTI / PAUSA / STOP / STATO, scrivendo su
control.json esattamente come fa control.py da riga di comando — sono due
telecomandi diversi per lo stesso bot, sempre sincronizzati.

Setup (una tantum):
  1. Su Telegram parla con @BotFather → /newbot → segui le istruzioni →
     copia il token che ti dà (tipo 123456789:ABC-...).
  2. Metti quel valore in TELEGRAM_BOT_TOKEN (.env in locale, Variables su Railway).
  3. Apri una chat con il TUO bot appena creato e mandagli un messaggio
     qualsiasi (es. "ciao").
  4. Lancia `python telegram_setup.py` per leggere il tuo chat_id e copialo
     in TELEGRAM_CHAT_ID (.env / Railway).
Senza queste due variabili l'integrazione resta disattivata: il bot continua
a funzionare normalmente, semplicemente senza notifiche Telegram.

Sicurezza: SOLO i messaggi provenienti dalla chat con id = TELEGRAM_CHAT_ID
vengono accettati come comandi. Chiunque altro scriva al bot (se qualcuno
scopre il suo username) riceve un rifiuto silenzioso, loggato ma senza dargli
alcuna informazione o controllo.
"""

import asyncio
import json
import logging
import time

import aiohttp

from config import CONFIG

log = logging.getLogger("telegram")

COMANDI = {
    "parti": "run", "start": "run", "/start": "run", "avvia": "run",
    "stop": "stop", "/stop": "stop", "ferma": "stop",
    "pausa": "pausa", "/pausa": "pausa", "pause": "pausa",
    "stato": "stato", "/stato": "stato", "status": "stato", "/status": "stato",
}

RISPOSTE_COMANDO = {
    "run":   "✅ <b>PARTI</b> ricevuto: operazioni normali (nuovi ingressi + monitoraggio).",
    "pausa": "⏸️ <b>PAUSA</b> ricevuta: nessun nuovo ingresso, le posizioni aperte restano protette (stop loss/trailing/ladder attivi).",
    "stop":  "🛑 <b>STOP</b> ricevuto: tutto fermo, incluso il monitoraggio automatico. Le posizioni aperte NON sono più protette finché non mandi di nuovo PARTI o PAUSA.",
}


class TelegramNotifier:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.token = CONFIG.api.telegram_bot_token
        self.chat_id = str(CONFIG.api.telegram_chat_id) if CONFIG.api.telegram_chat_id else ""
        self.attivo = bool(self.token and self.chat_id)
        self._offset = 0
        if not self.attivo:
            log.info("Telegram non configurato (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID mancanti): notifiche disattivate.")

    @property
    def _base_url(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    # ---------------- NOTIFICHE IN USCITA ----------------

    async def invia(self, testo: str):
        if not self.attivo:
            return
        try:
            async with self.session.post(
                f"{self._base_url}/sendMessage",
                json={"chat_id": self.chat_id, "text": testo, "parse_mode": "HTML"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    log.warning("Telegram sendMessage HTTP %s: %s", r.status, await r.text())
        except Exception as e:
            log.warning("Errore invio notifica Telegram: %s", e)

    # ---------------- COMANDI IN ENTRATA ----------------

    def _scrivi_controllo(self, comando: str):
        with open(CONFIG.file_controllo, "w") as f:
            json.dump({"comando": comando, "ts": time.time()}, f)

    def _testo_stato(self) -> str:
        try:
            with open("stato_bot.json") as f:
                stato = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            stato = {}
        try:
            with open(CONFIG.file_controllo) as f:
                comando = json.load(f).get("comando", "run")
        except (FileNotFoundError, json.JSONDecodeError):
            comando = "run"

        capitale = stato.get("capitale_eur")
        if capitale is None:
            return f"🎛️ Comando attivo: <b>{comando.upper()}</b>\nNessuno stato ancora disponibile (bot appena avviato)."
        n_pos = len(stato.get("posizioni", {}))
        return (
            f"🎛️ Comando attivo: <b>{comando.upper()}</b>\n"
            f"💰 Capitale tracciato: {capitale:.2f}€\n"
            f"📊 Posizioni aperte: {n_pos}\n"
            f"📈 Modalità: {CONFIG.mode.upper()}"
        )

    async def _gestisci_messaggio(self, msg: dict):
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        testo = (msg.get("text") or "").strip().lower()
        if not testo:
            return

        if chat_id != self.chat_id:
            log.warning("Comando Telegram ignorato da chat non autorizzata (%s): %r", chat_id, testo)
            return

        azione = COMANDI.get(testo)
        if azione is None:
            await self.invia("Comando non riconosciuto. Usa: PARTI, PAUSA, STOP, STATO.")
            return

        if azione == "stato":
            await self.invia(self._testo_stato())
            return

        self._scrivi_controllo(azione)
        await self.invia(RISPOSTE_COMANDO[azione])
        log.warning("🎛️ Comando '%s' ricevuto via Telegram", azione.upper())

    async def poll_comandi(self):
        """Long-polling su getUpdates. Va lanciato come task separato, gira
        per tutta la vita del processo. Non blocca né rallenta il loop di
        trading: è un task asyncio indipendente."""
        if not self.attivo:
            return
        log.info("📲 Telegram attivo: in ascolto comandi sulla chat configurata.")
        while True:
            try:
                async with self.session.get(
                    f"{self._base_url}/getUpdates",
                    params={"offset": self._offset, "timeout": 25},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as r:
                    if r.status != 200:
                        log.warning("Telegram getUpdates HTTP %s", r.status)
                        await asyncio.sleep(5)
                        continue
                    data = await r.json()
                for upd in data.get("result", []):
                    self._offset = upd["update_id"] + 1
                    msg = upd.get("message")
                    if msg:
                        await self._gestisci_messaggio(msg)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                log.warning("Errore polling Telegram: %s", e)
                await asyncio.sleep(5)
