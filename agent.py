"""
agent.py — Supervisore automatico del bot, 100% locale (nessuna chiamata AI).

Ogni 6 ore analizza i trade chiusi e, SOLO se i dati lo giustificano,
regola i parametri — applicati esclusivamente dentro i BOUNDS di sicurezza
hard-coded qui sotto. Le stesse regole che prima venivano affidate al
giudizio di un LLM sono qui codificate esplicitamente: possono ottimizzare,
non possono mai rendere il bot più aggressivo/pericoloso.

Principi (identici alla versione precedente, ora if/else invece che prompt):
1. Con meno di 15 trade chiusi, NON modificare nulla: il campione è troppo piccolo.
2. Se le perdite vengono soprattutto da stop loss immediati → i filtri d'ingresso
   sono laschi: alza score minimo e liquidità minima.
3. Se le perdite vengono da uscite a tempo/trailing senza mai raggiungere il
   ladder di take-profit → il timing d'ingresso è debole: alza il momentum minimo.
4. Se molte posizioni escono in ladder con buon win rate → il sistema funziona: non toccare.
5. Preferisce SEMPRE rendere il bot più selettivo (soglie più alte), mai più aggressivo.
6. Massimo 2 parametri modificati per sessione.

BOUNDS (non negoziabili, fuori dalla portata di qualunque logica automatica):
  size max 20%, stop loss tra -15% e -35%, score minimo 60-95,
  sentiment minimo 45-90, momentum minimo 30-85. Il circuit breaker
  giornaliero, il floor di sicurezza sul saldo reale e il paper/live mode
  NON sono modificabili automaticamente.
"""

import json
import logging
import time

from config import CONFIG

log = logging.getLogger("agent")

# (min, max) per ogni parametro che la logica automatica può toccare
BOUNDS = {
    "max_posizione_pct":   (0.05, 0.20),
    "stop_loss_pct":       (-0.35, -0.15),
    "min_claude_score":    (60, 95),
    "min_sentiment_score": (45, 90),
    "min_momentum_score":  (30, 85),
    "min_liquidity_usd":   (15_000, 100_000),
    "calm_soglia_vendita": (45.0, 80.0),
}

SOGLIA_TRADE_MINIMI = 15


class AgentSupervisor:
    def __init__(self, session, risk_manager):
        # `session` non serve più (nessuna chiamata di rete): si mantiene il
        # parametro solo per non rompere la firma usata da main.py.
        self.session = session
        self.risk = risk_manager
        self.ultimo_run = 0.0
        self.intervallo_sec = 6 * 3600

    # ---------------- STATISTICHE E PARAMETRI ----------------

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
                rifiutati[nome] = "parametro non modificabile automaticamente"
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
            if nome in ("max_posizione_pct", "stop_loss_pct", "calm_soglia_vendita"):
                setattr(CONFIG.risk, nome, v)
            elif nome == "min_claude_score":
                CONFIG.min_claude_score = int(v)
            else:
                setattr(CONFIG.filters, nome, int(v) if nome != "min_momentum_score" else v)
            applicati[nome] = v
        if applicati:
            log.info("🤖 Supervisore: parametri aggiornati %s", applicati)
            with open("agent_log.jsonl", "a") as f:
                f.write(json.dumps({"ts": time.time(), "applicati": applicati, "rifiutati": rifiutati}) + "\n")
        return {"applicati": applicati, "rifiutati": rifiutati}

    # ---------------- LOGICA DI DECISIONE (locale, deterministica) ----------------

    def _decidi(self, stat: dict) -> tuple[str, dict]:
        """Applica le regole descritte nel docstring di modulo.
        Ritorna (verdetto testuale in italiano, risultato di _applica)."""
        n = stat.get("trade_chiusi", 0)
        if n < SOGLIA_TRADE_MINIMI:
            verdetto = (
                f"OSSERVAZIONE: {n} trade chiusi finora.\n"
                f"DIAGNOSI: campione troppo piccolo per essere significativo (servono almeno {SOGLIA_TRADE_MINIMI}).\n"
                f"DECISIONE: Nessuna modifica.\n"
                f"PERCHÉ: con un campione così piccolo qualunque aggiustamento sarebbe rumore statistico, non segnale.\n"
                f"COSA GUARDARE: accumula altri {max(0, SOGLIA_TRADE_MINIMI - n)} trade chiusi prima della prossima revisione utile."
            )
            return verdetto, {"applicati": {}, "rifiutati": {}}

        win_rate = stat.get("win_rate", 0.0)
        motivi = stat.get("motivi_uscita", {})
        stop_loss_share = motivi.get("stop_loss", 0) / n
        trailing_time_share = motivi.get("trailing_o_time", 0) / n
        ladder_share = motivi.get("ladder", 0) / n

        proposte: dict = {}
        if win_rate < 0.35 and stop_loss_share >= 0.5:
            proposte["min_claude_score"] = min(BOUNDS["min_claude_score"][1], CONFIG.min_claude_score + 5)
            proposte["min_liquidity_usd"] = min(BOUNDS["min_liquidity_usd"][1], round(CONFIG.filters.min_liquidity_usd * 1.15))
            diagnosi = f"la maggior parte delle perdite ({stop_loss_share:.0%}) viene da stop loss immediati: i filtri d'ingresso sono troppo permissivi."
            perche = "alzare lo score minimo e la liquidità minima riduce i token deboli che entrano e finiscono subito in stop loss."
        elif win_rate < 0.40 and trailing_time_share >= 0.5 and ladder_share < 0.15:
            proposte["min_momentum_score"] = min(BOUNDS["min_momentum_score"][1], CONFIG.filters.min_momentum_score + 5)
            diagnosi = f"poche posizioni raggiungono il ladder di take-profit ({ladder_share:.0%}), la maggior parte esce per tempo/trailing senza guadagno: probabile timing d'ingresso debole."
            perche = "alzare il momentum minimo richiesto in ingresso filtra i token comprati senza una vera accelerazione in corso."
        elif win_rate >= 0.55 and ladder_share >= 0.2:
            diagnosi = f"win rate {win_rate:.0%} con {ladder_share:.0%} di uscite da ladder di take-profit: il sistema sta funzionando."
            perche = "non c'è motivo di cambiare parametri che stanno già producendo risultati positivi."
        else:
            diagnosi = "nessun pattern abbastanza netto nei dati per giustificare un aggiustamento con sicurezza."
            perche = "meglio non modificare sulla base di un segnale ambiguo: rischia di peggiorare le cose senza una causa identificata con certezza."

        risultato = self._applica(proposte) if proposte else {"applicati": {}, "rifiutati": {}}
        decisione_str = (
            ", ".join(f"{k} → {v}" for k, v in risultato["applicati"].items())
            if risultato["applicati"] else "Nessuna modifica."
        )
        verdetto = (
            f"OSSERVAZIONE: {n} trade chiusi, win rate {win_rate:.0%}, PnL totale {stat.get('pnl_totale_eur', 0):+.2f}€. "
            f"Motivi di uscita: {motivi}.\n"
            f"DIAGNOSI: {diagnosi}\n"
            f"DECISIONE: {decisione_str}\n"
            f"PERCHÉ: {perche}\n"
            f"COSA GUARDARE: confronta win rate e distribuzione dei motivi di uscita nei prossimi trade "
            f"con questi valori per capire se il cambiamento ha aiutato."
        )
        return verdetto, risultato

    # ---------------- SCHEDULAZIONE ----------------

    async def forse_esegui(self):
        if time.time() - self.ultimo_run < self.intervallo_sec:
            return
        self.ultimo_run = time.time()
        try:
            stat = self._statistiche()
            parametri_prima = self._parametri()["correnti"]
            verdetto, risultato = self._decidi(stat)
            log.info("🤖 Verdetto supervisore: %s", verdetto.replace("\n", " ")[:300])
            self._scrivi_report(verdetto, {
                "statistiche_lette": stat,
                "parametri_prima": parametri_prima,
                "modifiche": risultato,
            })
        except Exception as e:
            log.error("Errore supervisore automatico: %s", e)

    def _scrivi_report(self, verdetto: str, traccia: dict):
        """Scrive un report leggibile in report_agente.md (ultimo run in cima)."""
        ts = time.strftime("%Y-%m-%d %H:%M")
        stat = traccia.get("statistiche_lette") or {}
        mod = traccia.get("modifiche") or {}
        applicati = mod.get("applicati") or {}
        rifiutati = mod.get("rifiutati") or {}

        righe = [f"# Report supervisore — {ts}", ""]

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

        righe += ["## Ragionamento del supervisore", "", verdetto or "_(nessun verdetto)_", ""]
        righe += ["---", ""]

        nuovo = "\n".join(righe)
        try:
            with open("report_agente.md") as f:
                vecchio_contenuto = f.read()
        except FileNotFoundError:
            vecchio_contenuto = ""
        with open("report_agente.md", "w") as f:
            f.write(nuovo + "\n" + vecchio_contenuto)
        log.info("📄 Report supervisore scritto in report_agente.md")
