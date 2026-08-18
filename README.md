# Memecoin Bot — Solana (pump.fun / Raydium via Jupiter)

Bot automatico h24 per memecoin Solana, con filtri anti-rug e analisi
quantitativa 100% locale (nessuna AI esterna, nessun costo per-token).

## Struttura
```
├── main.py             # orchestratore: tre loop indipendenti
├── config.py           # TUTTI i parametri, con i valori misurati che li giustificano
├── scanner.py          # discovery su Jupiter Tokens v2 (3 feed uniti)
├── filters.py          # filtri anti-rug: percorso veloce + deep check pre-acquisto
├── local_analyzer.py   # score 0-100 su 5 dimensioni
├── timing.py           # momentum d'ingresso
├── sentiment.py        # presenza social strutturale
├── market_conditions.py# prezzi in batch + market calm score
├── market_sentiment.py # regime di mercato (Fear&Greed + SOL 24h)
├── risk_manager.py     # sizing, SL/TP/trailing, circuit breaker
├── executor.py         # swap via Jupiter, firma con solders, conferma on-chain
├── ratelimit.py        # limitatore per fornitore + backoff sui 429
├── metrics.py          # imbuto degli scarti (lo strumento di taratura)
├── agent.py            # supervisore automatico ogni 6h (regole locali)
├── control.py          # comandi manuali start / pausa / stop
├── telegram_bot.py     # notifiche + comandi via Telegram
├── dashboard.py        # dashboard web su http://localhost:8050
└── analizza_segnali.py # win rate per fascia di score, dopo il paper trading
```

## Setup
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # poi compila le chiavi
python main.py          # DEFAULT: PAPER MODE, nessun capitale a rischio
```

## ⚠️ Il default è PAPER, non LIVE
Il bot parte in simulazione. Per operare con soldi veri serve `BOT_MODE=live`
esplicito nel `.env`. In live, al primissimo avvio (nessuno `stato_bot.json`
precedente) capitale iniziale, circuit breaker e floor di sicurezza vengono
ancorati automaticamente al saldo SOL reale del wallet.

Prima di passare a live: lascia girare qualche giorno in paper, poi
`python analizza_segnali.py` per vedere quali fasce di score vincono davvero.

## Architettura: tre loop indipendenti

| Loop | Cadenza | Cosa fa |
|---|---|---|
| `loop_discovery` | 3 s | scan → filtri → scoring → cluster → acquisto |
| `loop_monitor` | 2 s | prezzi in batch → stop loss / ladder / trailing |
| `loop_sorveglianza` | 20 s | comando manuale, floor di sicurezza, supervisore |

Le vendite girano in task separati con un lock per mint: il loop di
monitoraggio registra la decisione e prosegue, senza restare appeso
all'esecuzione di uno swap.

## Fonti dati e budget di richieste

Il carico è diviso su due fornitori con limiti **separati**, perché il piano
gratuito Jupiter concede 60 richieste al minuto in tutto:

- **Jupiter Tokens v2** → discovery. Un'unica richiesta per feed restituisce
  prezzo, liquidità, market cap, statistiche 5m/1h/24h con volume organico
  separato, audit on-chain (mint/freeze authority, concentrazione top holder,
  quota e storico del dev) e link social. ~15 richieste/minuto.
- **DexScreener** → monitoraggio prezzi delle posizioni, in batch da 30 mint,
  su un budget indipendente. ~30 richieste/minuto.
- **Jupiter Swap v1** → quote ed esecuzione.
- **RugCheck** → un solo deep check, sul token che sta per essere comprato.

`ratelimit.py` fa rispettare i budget e gestisce i 429 con backoff. **Questo
non è un dettaglio**: superando il tetto, Jupiter risponde 429, il codice
riceve liste vuote e il bot continua a girare senza comprare mai, senza un
solo errore a log.

Per andare più veloce serve una API key Jupiter: metti
`JUPITER_BASE_URL=https://api.jup.ag` nel `.env` e alza i limiti in
`config.py`.

## Tarare il bot: l'imbuto degli scarti

Ogni 2 minuti il bot scrive dove muoiono i candidati, su log e in
`imbuto.jsonl`:

```
IMBUTO (ultimi 152s) — 228 valutati, 3 promossi (1.3%)
     80  metriche: pool troppo giovane
     52  audit_onchain: dev con lanci seriali
     31  metriche: liquidità sotto soglia
     26  eta: pool troppo vecchio
     13  audit_onchain: mint authority non revocata
      7  momentum: momentum sotto soglia
```

**Questo è il modo corretto di ottimizzare i parametri**: si guarda quale
stadio è il collo di bottiglia e si tocca quello, invece di cambiare soglie a
intuito. Se il bot non compra, la prima cosa da leggere è questo report — dice
se il problema è il mercato, una soglia troppo stretta o un rate limit.

La manopola della frequenza sono tre parametri in `config.py`
(`min_liquidity_usd`, `min_eta_pool_minuti`, `min_volume_5m_usd`), documentati
lì con i valori misurati e il compromesso di rischio che comportano.

## Controllo manuale
```bash
python control.py start   # ingressi + monitoraggio
python control.py pausa   # nessun nuovo ingresso, posizioni protette
python control.py stop    # tutto fermo, posizioni NON protette
python control.py stato   # comando attivo
```
Il bot legge `control.json` ad ogni ciclo: non serve riavviare. Gli stessi
comandi funzionano via Telegram (PARTI / PAUSA / STOP / STATO).

## Protezioni anti-perdita di capitale
- **Conferma on-chain reale**: ogni transazione viene attesa fino a conferma;
  l'importo ricevuto è letto dai balance pre/post della transazione, mai dalla
  quote preventivata.
- **Recupero da conferma tardiva**: se la conferma di un acquisto scade, il bot
  rilegge il saldo token on-chain prima di dichiarare fallimento — così non
  restano token reali fuori dal monitoraggio, senza stop loss.
- **Controllo saldo pre-trade** sul saldo SOL reale, non sul file di stato.
- **Contabilità sull'eseguito reale**: le vendite aggiornano la posizione solo
  per ciò che è stato davvero venduto e confermato.
- **Tier del ladder marcati solo dopo la conferma**: un take-profit la cui
  vendita è fallita resta disponibile al tentativo successivo.
- **Stop di sicurezza indipendente**: se il valore totale del wallet (SOL +
  valore corrente delle posizioni) scende sotto `floor_sicurezza_pct` del
  capitale iniziale, il bot si mette in pausa da solo e riparte solo con un
  `python control.py start` esplicito.

## Dashboard
```bash
python dashboard.py
```
Poi apri http://localhost:8050. Legge solo i file che il bot già produce e
mostra anche l'imbuto degli scarti e i parametri modificati dal supervisore.

## Nota su Photon
Photon è un terminale di trading **manuale**, senza API pubblica per bot.
Qualsiasi script che promette "integrazione Photon per bot" è da considerare
inaffidabile o malevolo (spesso ruba la chiave privata). Per la velocità
d'ingresso il bot usa Jito bundle con fallback automatico su RPC standard.

## Nota sull'"alta frequenza"
Il bot reagisce in secondi invece che in minuti, non in millisecondi. La
detection dei lanci via WebSocket è stata valutata e scartata su base
misurata: sottoscrivendo i programmi Pump.fun / PumpSwap / Raydium arrivano
372, 1178 e 38 transazioni al secondo, perché il filtro `mentions` cattura ogni
swap che tocca il programma e non è filtrabile per istruzione. Andrebbe
ingerito tutto per scartare il 99,9%, bruciando la quota RPC in poche ore. Il
feed `recent` di Jupiter espone gli stessi lanci (token presenti 4 secondi dopo
la creazione) al costo di una richiesta.

Con ~15 EUR per posizione il bot non vince una gara di latenza contro gli
sniper professionali con infrastruttura co-locata, e nessuna
riparametrizzazione cambia questo. I vantaggi reali qui sono la selettività dei
filtri e la disciplina delle uscite.

## Rischio
- Le memecoin sono l'asset più rischioso che esista: la perdita totale del
  capitale è lo scenario più probabile, non l'eccezione.
- Usa un wallet dedicato con SOLO il capitale che sei disposto a perdere.
- Le protezioni automatiche limitano il danno, non lo eliminano.
- Alzare la frequenza dei trade significa alzare l'esposizione ai rug: i token
  più giovani e sottili sono esattamente quelli dove i rug avvengono.
