# Memecoin Bot — Solana (Raydium/Pump.fun via Jupiter)

Bot automatico h24 per memecoin Solana con filtri anti-rug e analisi
quantitativa 100% locale (nessuna AI esterna, nessun costo per-token).

## Struttura
```
bot/
├── main.py             # orchestratore, loop principale
├── config.py           # tutti i parametri (rischio, filtri, API)
├── scanner.py          # rileva nuovi pool (DexScreener)
├── filters.py          # filtri anti-rug (RugCheck + metriche)
├── local_analyzer.py   # score 0-100 locale (vol/liq, buy/sell, mcap/liq, RugCheck, holder)
├── sentiment.py        # presenza social (segnali strutturali DexScreener, locale)
├── market_conditions.py# market calm score per le uscite in tranche
├── market_sentiment.py # regime di mercato aggregato (Fear&Greed Index + SOL 24h, locale)
├── agent.py             # supervisore automatico ogni 6h (regole locali, non AI)
├── dashboard.py         # server locale con dashboard web (http://localhost:8050)
├── dashboard_static/    # frontend della dashboard (index.html)
├── analizza_segnali.py  # analisi win rate per fascia di score (da usare dopo il paper trading)
├── executor.py         # swap via Jupiter, firma con solders
├── risk_manager.py     # position sizing, SL/TP, circuit breaker
├── control.py           # comandi manuali: start / pausa / stop
├── telegram_bot.py       # notifiche + comandi PARTI/PAUSA/STOP via Telegram
├── requirements.txt
└── .env.example        # template variabili d'ambiente
```

Nessun modulo chiama più API a pagamento: l'unica chiave a pagamento
possibile resta Helius (piano free già sufficiente). Fear&Greed Index e
prezzo SOL vengono letti da endpoint pubblici gratuiti senza API key.

## Setup rapido
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # poi compila le chiavi
python main.py          # parte in PAPER MODE
```

## Controllo manuale: start / pausa / stop
```bash
python control.py start   # operazioni normali (nuovi ingressi + monitoraggio)
python control.py pausa   # nessun nuovo ingresso, le posizioni aperte restano protette
python control.py stop    # tutto fermo, incluso il monitoraggio (posizioni non più protette)
python control.py stato   # mostra il comando attivo
```
Il bot legge `control.json` ad ogni ciclo: non serve riavviare il processo.
Su Railway, lancia questi comandi dalla tab **Console** del servizio.

## Protezioni anti-perdita di capitale
- **Conferma on-chain reale**: ogni transazione live viene attesa fino a conferma
  (`confirmed`/`finalized`) prima di essere considerata riuscita; l'importo
  ricevuto/speso viene letto dai balance pre/post della transazione, mai dalla
  quote preventivata.
- **Controllo saldo pre-trade**: prima di ogni acquisto il bot verifica il saldo
  SOL reale del wallet (non il file di stato interno) e rifiuta il trade se
  insufficiente.
- **Contabilità basata sull'eseguito reale**: le vendite a tranche aggiornano la
  posizione solo in base a ciò che è stato davvero venduto e confermato — se una
  tranche fallisce a metà, il residuo resta tracciato e riprovato, mai perso.
- **Stop di sicurezza indipendente**: se il saldo SOL reale del wallet scende
  sotto `floor_sicurezza_pct` (default 30%) del capitale iniziale, il bot si
  mette in pausa da solo, indipendentemente da cosa dice la contabilità interna.
  Riparte solo con `python control.py start`.

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
