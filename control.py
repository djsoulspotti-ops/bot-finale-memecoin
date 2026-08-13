"""
control.py — Comandi manuali per il bot: start, pausa, stop.

    python control.py start   # avvia/riprende operazioni normali (ingressi + monitoraggio)
                               # se il bot era fermo per lo STOP DI SICUREZZA (saldo
                               # reale sotto soglia), questo comando lo riconosce
                               # esplicitamente e sblocca il bot
    python control.py pausa   # ferma i nuovi ingressi; le posizioni già aperte
                               # restano protette (stop loss/trailing/ladder attivi)
    python control.py stop    # ferma TUTTO: nessun nuovo ingresso e nessun
                               # monitoraggio automatico. Le posizioni aperte NON
                               # sono più protette — usalo solo se vuoi gestirle
                               # tu manualmente da un wallet/exchange
    python control.py stato   # mostra il comando attivo

Il bot (main.py) legge control.json ad ogni ciclo (~20s) e si adegua subito,
senza bisogno di riavviare il processo.

Su Railway: apri la tab "Console" del servizio e lancia questi comandi nel
container in esecuzione (stessa working directory del bot, quindi vede lo
stesso control.json).
"""

import json
import sys
import time
from pathlib import Path

FILE = Path(__file__).parent / "control.json"
COMANDI_VALIDI = {"run", "pausa", "stop"}
ALIAS = {"start": "run", "avvia": "run", "resume": "run"}

MESSAGGI = {
    "run":   "✅ Bot avviato/ripreso: operazioni normali (nuovi ingressi + monitoraggio).",
    "pausa": "⏸️  Bot in pausa: NESSUN nuovo ingresso. Le posizioni aperte restano protette "
             "(stop loss, trailing stop e ladder di uscita continuano a funzionare).",
    "stop":  "🛑 Bot fermato del tutto: nessun nuovo ingresso E nessun monitoraggio automatico. "
             "Le posizioni aperte NON sono più protette da stop loss/trailing automatici — "
             "gestiscile tu manualmente finché non fai 'python control.py start' o 'pausa'.",
}


def leggi() -> dict:
    try:
        with open(FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"comando": "run", "ts": time.time()}


def scrivi(comando: str):
    with open(FILE, "w") as f:
        json.dump({"comando": comando, "ts": time.time()}, f)


def main():
    args = sys.argv[1:]
    cmd = ALIAS.get(args[0], args[0]) if args else None
    if cmd is None or cmd not in COMANDI_VALIDI | {"stato"}:
        print(__doc__)
        return

    if cmd == "stato":
        stato = leggi()
        ora = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stato.get("ts", time.time())))
        print(f"Comando attivo: {stato.get('comando', 'run').upper()}  (impostato il {ora})")
        return

    scrivi(cmd)
    print(MESSAGGI[cmd])


if __name__ == "__main__":
    main()
