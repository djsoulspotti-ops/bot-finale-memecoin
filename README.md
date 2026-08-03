# Memecoin Bot — Solana (Raydium/Pump.fun via Jupiter)

Bot automatico h24 per memecoin Solana con filtri anti-rug e analisi AI via API Claude.

## Struttura
```
bot/
├── main.py             # orchestratore, loop principale
├── config.py           # tutti i parametri (rischio, filtri, API)
├── scanner.py          # rileva nuovi pool (DexScreener)
├── filters.py          # filtri anti-rug (RugCheck + metriche)
├── claude_analyzer.py  # analisi qualitativa via API Anthropic
├── sentiment.py        # sentiment social (segnali strutturali + web search)
├── market_conditions.py# market calm score per le uscite in tranche
├── market_sentiment.py # sentiment AGGREGATO del mercato memecoin (regime globale)
├── dashboard.py         # server locale con dashboard web (http://localhost:8050)
├── dashboard_static/    # frontend della dashboard (index.html)
├── analizza_segnali.py  # analisi win rate per fascia di score (da usare dopo il paper trading)
├── executor.py         # swap via Jupiter, firma con solders
├── risk_manager.py     # position sizing, SL/TP, circuit breaker
├── requirements.txt
└── .env.example        # template variabili d'ambiente
```

## Setup rapido
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # poi compila le chiavi
python main.py          # parte in PAPER MODE
```

## Deploy h24 su VPS (systemd)
Vedi il documento di architettura per il file `memecoin-bot.service` completo.

## Dashboard
```bash
python dashboard.py
# poi apri http://localhost:8050 nel browser
```
Legge solo i file che il bot già produce (nessuna modifica al bot necessaria).
Si aggiorna da sola ogni 10 secondi.

## Nota su Photon
Photon (photon-sol.tinyastro.io) è un terminale di trading **manuale**, senza
API pubblica per bot. Qualsiasi script che promette "integrazione Photon per
bot" è da considerare inaffidabile o malevolo (spesso ruba la chiave privata).
Per velocità d'ingresso reale il bot usa Jito bundle (via Jupiter, supporto
nativo) con fallback automatico su RPC standard — vedi `config.py: usa_jito`.


- Parti SEMPRE in paper mode per almeno 2 settimane.
- Usa un wallet dedicato con SOLO il capitale del bot.
- Le memecoin sono l'asset più rischioso che esista: la perdita totale
  dei 100 EUR è lo scenario più probabile, non l'eccezione.
