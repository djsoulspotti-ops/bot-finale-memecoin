"""
analizza_segnali.py — Da lanciare dopo un po' di paper trading (min. 20-30 trade).

Legge trade_chiusi.jsonl e mostra il win rate e il PnL medio per fascia di
ogni segnale d'ingresso (Claude score, sentiment, momentum, regime di mercato).

Questo è il modo corretto di "ottimizzare" il bot: non cambiare i parametri
a intuito, ma vedere nei DATI REALI se, per esempio, i trade con momentum > 70
vincono nettamente più spesso di quelli sotto 60 — e SOLO in quel caso alzare
la soglia in config.py con una base solida.

Uso:
    python analizza_segnali.py
"""

import json
from collections import defaultdict

FILE = "trade_chiusi.jsonl"


def carica_trade() -> list[dict]:
    trades = []
    try:
        with open(FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        trades.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except FileNotFoundError:
        print(f"File {FILE} non trovato: nessun trade chiuso ancora.")
    return trades


def stampa_tabella(titolo: str, righe: list[tuple]):
    print(f"\n{titolo}")
    print("-" * len(titolo))
    print(f"{'Fascia':<20s} {'N.trade':>8s} {'Win rate':>10s} {'PnL medio':>12s} {'PnL totale':>12s}")
    for fascia, n, wr, pnl_medio, pnl_tot in righe:
        print(f"{fascia:<20s} {n:>8d} {wr:>9.0f}% {pnl_medio:>+11.2f}€ {pnl_tot:>+11.2f}€")


def analizza_per_fascia(trades: list[dict], campo: str, soglie: list[tuple]) -> list[tuple]:
    """soglie: lista di (etichetta, min, max) — max escluso, usare None per infinito"""
    righe = []
    for etichetta, lo, hi in soglie:
        sub = [t for t in trades
               if t.get(campo) is not None
               and t[campo] >= lo
               and (hi is None or t[campo] < hi)]
        if not sub:
            righe.append((etichetta, 0, 0, 0, 0))
            continue
        vincenti = [t for t in sub if t["pnl_eur"] > 0]
        wr = len(vincenti) / len(sub) * 100
        pnl_medio = sum(t["pnl_eur"] for t in sub) / len(sub)
        pnl_tot = sum(t["pnl_eur"] for t in sub)
        righe.append((etichetta, len(sub), wr, pnl_medio, pnl_tot))
    return righe


def analizza_per_categoria(trades: list[dict], campo: str) -> list[tuple]:
    gruppi = defaultdict(list)
    for t in trades:
        val = t.get(campo)
        if val is not None:
            gruppi[val].append(t)
    righe = []
    for cat, sub in sorted(gruppi.items()):
        vincenti = [t for t in sub if t["pnl_eur"] > 0]
        wr = len(vincenti) / len(sub) * 100
        pnl_medio = sum(t["pnl_eur"] for t in sub) / len(sub)
        pnl_tot = sum(t["pnl_eur"] for t in sub)
        righe.append((str(cat), len(sub), wr, pnl_medio, pnl_tot))
    return righe


def main():
    trades = carica_trade()
    if not trades:
        return

    print(f"Trade totali analizzati: {len(trades)}")
    if len(trades) < 20:
        print("⚠️  Campione piccolo (<20 trade): le percentuali sotto sono indicative, non conclusive.")

    vincenti = [t for t in trades if t["pnl_eur"] > 0]
    print(f"Win rate complessivo: {len(vincenti) / len(trades) * 100:.0f}%")
    print(f"PnL totale: {sum(t['pnl_eur'] for t in trades):+.2f}€")

    soglie_score = [("< 60", 0, 60), ("60-70", 60, 70), ("70-80", 70, 80), ("80-90", 80, 90), (">= 90", 90, None)]
    soglie_momentum = [("< 50", 0, 50), ("50-65", 50, 65), ("65-80", 65, 80), (">= 80", 80, None)]

    # `score_locale` è il nome attuale; `claude_score` resta letto per non
    # perdere i trade registrati dalle versioni precedenti del bot.
    for t in trades:
        if t.get("score_locale") is None and t.get("claude_score") is not None:
            t["score_locale"] = t["claude_score"]

    if any(t.get("score_locale") is not None for t in trades):
        stampa_tabella("Win rate per fascia di SCORE LOCALE",
                       analizza_per_fascia(trades, "score_locale", soglie_score))
    if any(t.get("sentiment_score") is not None for t in trades):
        stampa_tabella("Win rate per fascia di SENTIMENT SCORE", analizza_per_fascia(trades, "sentiment_score", soglie_score))
    if any(t.get("momentum_score") is not None for t in trades):
        stampa_tabella("Win rate per fascia di MOMENTUM SCORE", analizza_per_fascia(trades, "momentum_score", soglie_momentum))
    if any(t.get("regime_entrata") is not None for t in trades):
        stampa_tabella("Win rate per REGIME DI MERCATO all'ingresso", analizza_per_categoria(trades, "regime_entrata"))
    if any(t.get("motivo") is not None for t in trades):
        stampa_tabella("Win rate per MOTIVO DI USCITA", analizza_per_categoria(trades, "motivo"))

    print("\nCome leggere questi numeri:")
    print("- Con meno di ~10 trade per fascia, il win rate è rumore, non segnale.")
    print("- Cerca fasce dove il PnL medio è chiaramente positivo E il numero di trade")
    print("  è sufficiente per fidarsene (idealmente 15+): quella è la soglia da alzare")
    print("  in config.py, non un numero scelto a intuito.")


if __name__ == "__main__":
    main()
