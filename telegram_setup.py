"""
telegram_setup.py — Trova il tuo TELEGRAM_CHAT_ID (da fare una volta sola).

Passi:
  1. Metti TELEGRAM_BOT_TOKEN in .env (il token che ti ha dato @BotFather).
  2. Su Telegram, apri una chat con il tuo bot e mandagli un messaggio
     qualsiasi (es. "ciao").
  3. Lancia: python telegram_setup.py
  4. Copia il chat_id stampato in TELEGRAM_CHAT_ID (.env in locale,
     Variables su Railway).

Non serve nessuna libreria extra: usa solo la libreria standard.
"""

import json
import os
import sys
import urllib.request

from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")


def main():
    if not TOKEN:
        print("TELEGRAM_BOT_TOKEN non impostato in .env — mettilo prima di lanciare questo script.")
        sys.exit(1)

    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"Errore contattando Telegram: {e}")
        sys.exit(1)

    if not data.get("ok"):
        print(f"Telegram ha risposto con un errore: {data}")
        sys.exit(1)

    risultati = data.get("result", [])
    if not risultati:
        print("Nessun messaggio trovato. Apri una chat col tuo bot su Telegram, "
              "mandagli un messaggio qualsiasi, poi rilancia questo script.")
        return

    ultimo = risultati[-1]
    chat = (ultimo.get("message") or ultimo.get("channel_post") or {}).get("chat", {})
    chat_id = chat.get("id")
    nome = chat.get("first_name") or chat.get("title") or "?"

    if chat_id is None:
        print("Non sono riuscito a leggere il chat_id dall'ultimo messaggio. Riprova.")
        return

    print(f"Chat trovata: {nome} (id={chat_id})")
    print(f"\nCopia questo valore in TELEGRAM_CHAT_ID:\n{chat_id}")


if __name__ == "__main__":
    main()
