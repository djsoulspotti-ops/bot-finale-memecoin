"""
agent.py — Supervisore agentico del bot.

Ogni 6 ore Claude viene invocato come AGENTE (loop di tool use multi-step):
legge le statistiche dei trade, ispeziona i parametri correnti, ragiona,
e propone modifiche ai parametri — che vengono applicate SOLO se dentro
i bounds di sicurezza hard-coded qui sotto. L'agente può ottimizzare,
non può rendere il bot più pericoloso.

Tool esposti all'agente:
  - leggi_statistiche : win rate, PnL, distribuzione motivi di uscita
  - leggi_parametri   : valori correnti dei parametri regolabili
  - imposta_parametri : applica nuovi valori (validati contro i BOUNDS)

BOUNDS (non negoziabili, fuori dalla portata dell'agente):
  size max 20%, stop loss tra -15% e -35%, Claude score minimo 60,
  sentiment minimo 45, momentum minimo 30. Il circuit breaker giornaliero
  e il paper/live mode NON sono modificabili dall'agente.
"""

import json
import logging
import time

import aiohttp

from config import CONFIG

log = logging.getLogger("agent")

# (min, max) per ogni parametro che l'agente può toccare
BOUNDS = {
    "max_posizione_pct":   (0.05, 0.20),
    "stop_loss_pct":       (-0.35, -0.15),
    "min_claude_score":    (60, 95),
    "min_sentiment_score": (45, 90),
    "min_momentum_score":  (30, 85),
    "min_liquidity_usd":   (15_000, 100_000),
    "calm_soglia_vendita": (45.0, 80.0),
}

TOOLS = [
    {
        "name": "leggi_statistiche",
        "description": "Statistiche dei trade chiusi: numero, win rate, PnL totale, motivi di uscita.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "leggi_parametri",
        "description": "Valori correnti dei parametri regolabili e relativi bounds.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "imposta_parametri",
        "description": "Applica nuovi valori ai parametri. Solo i parametri nei bounds vengono accettati.",
        "input_schema": {
            "type": "object",
            "properties": {"parametri": {"type": "object", "description": "coppie nome: valore"}},
            "required": ["parametri"],
        },
    },
]

SYSTEM = """Sei il supervisore di un bot di trading su memecoin Solana in esecuzione continua.
Ogni 6 ore analizzi le performance e, SOLO se i dati lo giustificano, regoli i parametri.

Principi:
1. Con meno di 15 trade chiusi, NON modificare nulla: il campione è troppo piccolo.
2. Se le perdite vengono soprattutto da stop loss immediati → i filtri d'ingresso sono laschi: alza le soglie (claude/sentiment/momentum/liquidità).
3. Se le perdite vengono da time-exit → stai comprando token morti: alza il momentum minimo.
4. Se molte posizioni escono in trailing con buon profitto → il sistema funziona: non toccare.
5. Preferisci SEMPRE rendere il bot più selettivo che più aggressivo.
6. Massimo 2 parametri modificati per sessione, con variazioni piccole (≤20% del valore).

Il tuo verdetto finale (l'ultimo messaggio, quando non chiami più tool) DEVE seguire ESATTAMENTE questa struttura in italiano semplice, così un umano non tecnico può capirlo:

OSSERVAZIONE: <cosa dicono i dati, 1-2 frasi>
DIAGNOSI: <qual è il problema o il punto di forza, 1-2 frasi>
DECISIONE: <cosa hai cambiato e di quanto, oppure "Nessuna modifica" con il motivo>
PERCHÉ: <il ragionamento che collega diagnosi e decisione, 1-2 frasi>
COSA GUARDARE: <cosa dovrebbe osservare l'utente nelle prossime ore per capire se avevi ragione>"""


class AgentSupervisor:
    def __init__(self, session: aiohttp.ClientSession, risk_manager):
        self.session = session
        self.risk = risk_manager
        self.ultimo_run = 0.0
        self.intervallo_sec = 6 * 3600

    # ---------------- TOOL IMPLEMENTATIONS ----------------

    def _statistiche(self) -> dict:
        trades = []
        try:
            with open("trade_chiusi.jsonl") as f:
                trades = [json.loads(l) for l in f if l.strip()]
        except FileNotFoundError:
            pass
        if not trades:
            return {"trade_chiusi": 0, "nota": "nessun trade chiuso finora"}
        vincenti = [t for t in trades if t["pnl_eur"] > 0]
        motivi = {}
        for t in trades:
            motivi[t.get("motivo", "?")] = motivi.get(t.get("motivo", "?"), 0) + 1
        return {
            "trade_chiusi": len(trades),
            "win_rate": round(len(vincenti) / len(trades), 3),
            "pnl_totale_eur": round(sum(t["pnl_eur"] for t in trades), 2),
            "pnl_medio_vincenti": round(sum(t["pnl_eur"] for t in vincenti) / max(len(vincenti), 1), 2),
            "motivi_uscita": motivi,
            "capitale_corrente_eur": round(self.risk.capitale_eur, 2),
        }

    def _parametri(self) -> dict:
        return {
            "correnti": {
                "max_posizione_pct": CONFIG.risk.max_posizione_pct,
                "stop_loss_pct": CONFIG.risk.stop_loss_pct,
                "min_claude_score": CONFIG.min_claude_score,
                "min_sentiment_score": CONFIG.filters.min_sentiment_score,
                "min_momentum_score": CONFIG.filters.min_momentum_score,
                "min_liquidity_usd": CONFIG.filters.min_liquidity_usd,
                "calm_soglia_vendita": CONFIG.risk.calm_soglia_vendita,
            },
            "bounds": BOUNDS,
        }

    def _applica(self, parametri: dict) -> dict:
        applicati, rifiutati = {}, {}
        for nome, valore in parametri.items():
            if nome not in BOUNDS:
                rifiutati[nome] = "parametro non modificabile dall'agente"
                continue
            lo, hi = BOUNDS[nome]
            try:
                v = float(valore)
            except (TypeError, ValueError):
                rifiutati[nome] = "valore non numerico"
                continue
            if not (lo <= v <= hi):
                rifiutati[nome] = f"fuori bounds [{lo}, {hi}]"
                continue
            # Applica sul CONFIG vivo
            if nome in ("max_posizione_pct", "stop_loss_pct", "calm_soglia_vendita"):
                setattr(CONFIG.risk, nome, v)
            elif nome == "min_claude_score":
                CONFIG.min_claude_score = int(v)
            else:
                setattr(CONFIG.filters, nome, int(v) if nome != "min_momentum_score" else v)
            applicati[nome] = v
        if applicati:
            log.info("🤖 Agente: parametri aggiornati %s", applicati)
            with open("agent_log.jsonl", "a") as f:
                f.write(json.dumps({"ts": time.time(), "applicati": applicati, "rifiutati": rifiutati}) + "\n")
        return {"applicati": applicati, "rifiutati": rifiutati}

    # ---------------- AGENT LOOP ----------------

    async def forse_esegui(self):
        if time.time() - self.ultimo_run < self.intervallo_sec:
            return
        self.ultimo_run = time.time()
        try:
            await self._loop_agentico()
        except Exception as e:
            log.error("Errore supervisore agentico: %s", e)

    async def _loop_agentico(self, max_turni: int = 6):
        messages = [{"role": "user", "content": "Analizza le performance del bot e decidi se e come regolare i parametri."}]
        headers = {
            "x-api-key": CONFIG.api.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        # Traccia tutto ciò che l'agente ha visto e fatto, per il report
        traccia = {"statistiche_lette": None, "parametri_prima": None, "modifiche": None}

        for _ in range(max_turni):
            body = {
                "model": CONFIG.api.claude_model,
                "max_tokens": 1500,
                "system": SYSTEM,
                "messages": messages,
                "tools": TOOLS,
            }
            async with self.session.post(
                "https://api.anthropic.com/v1/messages", json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                if r.status != 200:
                    log.error("Agente HTTP %s: %s", r.status, await r.text())
                    return
                data = await r.json()

            messages.append({"role": "assistant", "content": data["content"]})

            if data.get("stop_reason") != "tool_use":
                verdetto = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
                log.info("🤖 Verdetto agente: %s", verdetto.strip()[:300])
                self._scrivi_report(verdetto.strip(), traccia)
                return

            # Esegui i tool richiesti, tracciando gli output rilevanti
            results = []
            for block in data["content"]:
                if block.get("type") != "tool_use":
                    continue
                nome, tid, inp = block["name"], block["id"], block.get("input", {})
                if nome == "leggi_statistiche":
                    out = self._statistiche()
                    traccia["statistiche_lette"] = out
                elif nome == "leggi_parametri":
                    out = self._parametri()
                    traccia["parametri_prima"] = out.get("correnti")
                elif nome == "imposta_parametri":
                    out = self._applica(inp.get("parametri", {}))
                    traccia["modifiche"] = out
                else:
                    out = {"errore": "tool sconosciuto"}
                results.append({"type": "tool_result", "tool_use_id": tid, "content": json.dumps(out)})
            messages.append({"role": "user", "content": results})

        log.warning("Agente: raggiunto il limite di turni senza verdetto finale")

    def _scrivi_report(self, verdetto: str, traccia: dict):
        """
        Scrive un report leggibile in report_agente.md (ultimo run in cima).
        Serve all'utente per CAPIRE le decisioni dell'agente, non per subirle.
        """
        ts = time.strftime("%Y-%m-%d %H:%M")
        stat = traccia.get("statistiche_lette") or {}
        mod = traccia.get("modifiche") or {}
        applicati = mod.get("applicati") or {}
        rifiutati = mod.get("rifiutati") or {}

        righe = [f"# Report supervisore — {ts}", ""]

        # Riepilogo dati in un colpo d'occhio
        if stat.get("trade_chiusi", 0) > 0:
            righe += [
                "## Dati al momento dell'analisi",
                f"- Trade chiusi: **{stat.get('trade_chiusi')}**",
                f"- Win rate: **{stat.get('win_rate', 0) * 100:.0f}%**",
                f"- PnL totale: **{stat.get('pnl_totale_eur', 0):+.2f} €**",
                f"- Capitale corrente: **{stat.get('capitale_corrente_eur', 0):.2f} €**",
                f"- Motivi di uscita: {stat.get('motivi_uscita', {})}",
                "",
            ]
        else:
            righe += ["## Dati al momento dell'analisi", "- Nessun trade chiuso ancora.", ""]

        # Cosa ha cambiato, in chiaro
        righe.append("## Modifiche applicate")
        if applicati:
            prima = traccia.get("parametri_prima") or {}
            for nome, valore in applicati.items():
                vecchio = prima.get(nome, "?")
                righe.append(f"- `{nome}`: {vecchio} → **{valore}**")
        else:
            righe.append("- Nessuna. I parametri restano invariati.")
        if rifiutati:
            righe.append("")
            righe.append("### Modifiche rifiutate (fuori dai limiti di sicurezza)")
            for nome, motivo in rifiutati.items():
                righe.append(f"- `{nome}`: {motivo}")
        righe.append("")

        # Il ragionamento dell'agente, in italiano
        righe += ["## Ragionamento dell'agente", "", verdetto or "_(nessun verdetto testuale)_", ""]
        righe += ["---", ""]

        # Prepend: l'ultimo report resta in cima al file
        nuovo = "\n".join(righe)
        try:
            with open("report_agente.md") as f:
                vecchio_contenuto = f.read()
        except FileNotFoundError:
            vecchio_contenuto = ""
        with open("report_agente.md", "w") as f:
            f.write(nuovo + "\n" + vecchio_contenuto)
        log.info("📄 Report supervisore scritto in report_agente.md")
