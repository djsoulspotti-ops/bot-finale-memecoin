"""
dashboard.py — Server locale che espone lo stato del bot in JSON.

Non modifica in alcun modo il bot: legge soltanto i file che main.py già
produce (stato_bot.json, trade_chiusi.jsonl, report_agente.md, bot.log)
e li serve alla pagina index.html. Gira sulla stessa macchina del bot.

Avvio:  python dashboard.py
Poi apri http://localhost:8050 nel browser (anche da un altro dispositivo
sulla stessa rete locale, sostituendo localhost con l'IP del server).
"""

import json
import os
import re
from pathlib import Path

from flask import Flask, jsonify, send_from_directory

app = Flask(__name__, static_folder=None)
BASE = Path(__file__).parent


def leggi_json_sicuro(path: str, default):
    try:
        with open(BASE / path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def leggi_jsonl_sicuro(path: str) -> list:
    righe = []
    try:
        with open(BASE / path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        righe.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except FileNotFoundError:
        pass
    return righe


def leggi_ultimo_report() -> dict:
    """Estrae il primo blocco di report_agente.md (l'ultimo run, sempre in cima)."""
    path = BASE / "report_agente.md"
    if not path.exists():
        return {"disponibile": False}
    testo = path.read_text()
    blocchi = testo.split("\n---\n")
    primo = blocchi[0].strip() if blocchi else ""
    if not primo:
        return {"disponibile": False}
    m = re.search(r"# Report supervisore — (.+)", primo)
    return {"disponibile": True, "timestamp": m.group(1) if m else "?", "markdown": primo}


def leggi_ultimo_regime() -> dict:
    """Ultima riga di tipo 'regime' loggata dal bot, se salvata; altrimenti sconosciuto."""
    return leggi_json_sicuro("ultimo_regime.json", {"regime": "SCONOSCIUTO", "score": None, "note": ""})


def leggi_ultimo_imbuto() -> dict:
    """Ultima finestra dell'imbuto degli scarti (scritta da metrics.py).

    È il dato che dice PERCHÉ il bot non sta comprando: senza sapere quale
    stadio scarta i candidati, guardare "0 trade" non dice niente di
    utile. Include anche l'andamento delle ultime finestre, per vedere se una
    modifica alle soglie ha spostato il collo di bottiglia.
    """
    righe = leggi_jsonl_sicuro("imbuto.jsonl")
    if not righe:
        return {"disponibile": False}
    ultima = righe[-1]
    storico = [{"ts": r.get("ts"), "valutati": r.get("valutati"),
                "promossi": r.get("promossi")} for r in righe[-30:]]
    return {"disponibile": True, "ultima": ultima, "storico": storico}


def coda_log(n: int = 200) -> list:
    path = BASE / "bot.log"
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        blocco = min(size, 200_000)  # ultimi ~200KB, sufficienti per n righe
        f.seek(size - blocco)
        testo = f.read().decode(errors="ignore")
    righe = testo.splitlines()[-n:]
    return righe


@app.route("/api/stato")
def api_stato():
    stato = leggi_json_sicuro("stato_bot.json", {"capitale_eur": None, "posizioni": {}})
    trades = leggi_jsonl_sicuro("trade_chiusi.jsonl")
    vincenti = [t for t in trades if t.get("pnl_eur", 0) > 0]
    win_rate = round(len(vincenti) / len(trades) * 100, 1) if trades else None
    pnl_totale = round(sum(t.get("pnl_eur", 0) for t in trades), 2)

    motivi = {}
    for t in trades:
        m = t.get("motivo", "?")
        motivi[m] = motivi.get(m, 0) + 1

    return jsonify({
        "capitale_eur": stato.get("capitale_eur"),
        "posizioni": stato.get("posizioni", {}),
        "n_posizioni": len(stato.get("posizioni", {})),
        "n_trade": len(trades),
        "win_rate": win_rate,
        "pnl_totale_eur": pnl_totale,
        "motivi_uscita": motivi,
        "trades_recenti": list(reversed(trades[-20:])),
        "regime": leggi_ultimo_regime(),
        "report_agente": leggi_ultimo_report(),
        "imbuto": leggi_ultimo_imbuto(),
        "parametri_runtime": leggi_json_sicuro("parametri_runtime.json", {}),
        "log_recente": coda_log(80),
    })


@app.route("/")
def index():
    return send_from_directory(BASE / "dashboard_static", "index.html")


@app.route("/<path:fname>")
def static_files(fname):
    return send_from_directory(BASE / "dashboard_static", fname)


if __name__ == "__main__":
    print("Dashboard su http://localhost:8050  (Ctrl+C per fermare)")
    app.run(host="0.0.0.0", port=8050, debug=False)
