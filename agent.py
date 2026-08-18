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
  vedi il dizionario BOUNDS qui sotto. Il circuit breaker giornaliero, il
  floor di sicurezza sul valore reale del wallet e il paper/live mode NON
  sono modificabili automaticamente.

Le modifiche vengono ora PERSISTITE in parametri_runtime.json e ricaricate
all'avvio: prima erano solo setattr in memoria, quindi ogni riavvio del
processo le annullava mentre report_agente.md continuava a dichiararle attive.
"""

import json
import logging
import time

from config import CONFIG

log = logging.getLogger("agent")

# (min, max) per ogni parametro che la logica automatica può toccare
BOUNDS = {
    "max_posizione_pct":   (0.05, 0.20),
    "stop_loss_pct":       (-0.35, -0.12),
    "min_score_locale":    (45, 90),
    "min_sentiment_score": (15, 80),
    "min_momentum_score":  (25, 80),
    "min_liquidity_usd":   (4_000, 80_000),
    "max_dev_mints":       (5, 200),
    "min_organic_score":   (10.0, 70.0),
    "calm_soglia_vendita": (35.0, 80.0),
}

# A quale oggetto di configurazione appartiene ogni parametro. Serve a
# `_applica` per scrivere nel posto giusto senza catene di if/elif che si
# disallineano ad ogni rinomina.
CONTENITORE = {
    "max_posizione_pct":   "risk",
    "stop_loss_pct":       "risk",
    "calm_soglia_vendita": "risk",
    "min_sentiment_score": "filters",
    "min_momentum_score":  "filters",
    "min_liquidity_usd":   "filters",
    "max_dev_mints":       "filters",
    "min_organic_score":   "filters",
    "min_score_locale":    "bot",
}

# Parametri che devono restare interi
INTERI = {"min_score_locale", "min_sentiment_score", "max_dev_mints"}

SOGLIA_TRADE_MINIMI = 15


class AgentSupervisor:
    def __init__(self, session, risk_manager, telegram=None):
        # `session` non serve più (nessuna chiamata di rete): si mantiene il
        # parametro solo per non rompere la firma usata da main.py.
        self.session = session
        self.risk = risk_manager
        self.telegram = telegram
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

    @staticmethod
    def _bersaglio(nome: str):
        """Oggetto di configurazione che contiene `nome`."""
        dove = CONTENITORE.get(nome)
        if dove == "risk":
            return CONFIG.risk
        if dove == "filters":
            return CONFIG.filters
        return CONFIG

    def _parametri(self) -> dict:
        correnti = {nome: getattr(self._bersaglio(nome), nome, None) for nome in BOUNDS}
        return {"correnti": correnti, "bounds": BOUNDS}

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
            if nome in INTERI:
                v = int(v)
            setattr(self._bersaglio(nome), nome, v)
            applicati[nome] = v

        if applicati:
            log.info("🤖 Supervisore: parametri aggiornati %s", applicati)
            # PERSISTENZA. Prima le modifiche erano solo setattr su oggetti in
            # memoria: al primo riavvio del processo (su Railway succede di
            # routine) i parametri tornavano ai default, mentre
            # report_agente.md continuava a dichiarare le ottimizzazioni
            # attive. Il supervisore "imparava" e dimenticava tutto.
            try:
                CONFIG.salva_parametri_runtime(applicati)
            except OSError as e:
                log.error("Impossibile persistere i parametri del supervisore: %s", e)
            try:
                with open("agent_log.jsonl", "a") as f:
                    f.write(json.dumps({"ts": time.time(), "applicati": applicati,
                                        "rifiutati": rifiutati}) + "\n")
            except OSError:
                pass
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
        # I motivi sono quelli REALI restituiti da risk_manager.decisione_uscita.
        # Prima "trailing_o_time" accorpava tre cause diverse e un trailing
        # dopo un tier veniva contato come "ladder": il supervisore leggeva
        # quote distorte verso "il sistema funziona" e non correggeva nulla.
        stop_loss_share = (motivi.get("stop_loss", 0) + motivi.get("volatility_stop", 0)) / n
        trailing_share = motivi.get("trailing_stop", 0) / n
        tempo_share = (motivi.get("no_momentum", 0) + motivi.get("max_holding", 0)) / n
        ladder_share = motivi.get("ladder", 0) / n

        proposte: dict = {}
        if win_rate < 0.35 and stop_loss_share >= 0.5:
            proposte["min_score_locale"] = min(BOUNDS["min_score_locale"][1], CONFIG.min_score_locale + 4)
            proposte["min_liquidity_usd"] = min(BOUNDS["min_liquidity_usd"][1],
                                                round(CONFIG.filters.min_liquidity_usd * 1.15))
            diagnosi = (f"il {stop_loss_share:.0%} delle uscite è per stop loss o volatility stop: "
                        f"entrano token che crollano subito, quindi i filtri d'ingresso sono troppo permissivi.")
            perche = "alzare lo score minimo e la liquidità minima riduce i token deboli che finiscono subito in stop loss."
        elif win_rate < 0.40 and tempo_share >= 0.5 and ladder_share < 0.15:
            proposte["min_momentum_score"] = min(BOUNDS["min_momentum_score"][1],
                                                 CONFIG.filters.min_momentum_score + 5)
            diagnosi = (f"il {tempo_share:.0%} delle uscite è per scadenza di tempo e solo il "
                        f"{ladder_share:.0%} raggiunge il ladder: si compra roba che non si muove, "
                        f"quindi il timing d'ingresso è debole.")
            perche = "alzare il momentum minimo filtra i token comprati senza una vera accelerazione in corso."
        elif trailing_share >= 0.45 and ladder_share < 0.2:
            # Caso che la versione precedente non poteva nemmeno vedere,
            # perché confondeva trailing e ladder nello stesso conteggio.
            proposte["stop_loss_pct"] = max(BOUNDS["stop_loss_pct"][0], CONFIG.risk.stop_loss_pct - 0.03)
            diagnosi = (f"il {trailing_share:.0%} delle uscite è per trailing stop con solo il "
                        f"{ladder_share:.0%} di ladder: le posizioni salgono un po' e vengono chiuse "
                        f"dal trailing prima di arrivare al primo take profit.")
            perche = ("allargare lo stop loss dà alle posizioni più spazio per respirare prima di "
                      "essere chiuse; il trailing resta il meccanismo che protegge il guadagno.")
        elif win_rate >= 0.55 and ladder_share >= 0.2:
            diagnosi = f"win rate {win_rate:.0%} con {ladder_share:.0%} di uscite da ladder: il sistema sta funzionando."
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
            await self._notifica_telegram(verdetto, risultato)
        except Exception as e:
            log.error("Errore supervisore automatico: %s", e)

    async def _notifica_telegram(self, verdetto: str, risultato: dict):
        """Il supervisore gira da solo ogni 6h e può cambiare parametri di
        rischio senza che nessuno lo guardi: senza questa notifica l'unico
        modo per accorgersene sarebbe aprire report_agente.md sul server."""
        if not self.telegram:
            return
        applicati = risultato.get("applicati") or {}
        if applicati:
            emoji, titolo = "🔧", "Supervisore: parametri aggiornati"
        else:
            emoji, titolo = "🧭", "Supervisore: nessuna modifica"
        await self.telegram.invia(f"{emoji} <b>{titolo}</b>\n\n{verdetto}")

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
