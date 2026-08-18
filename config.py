"""
config.py — Configurazione centrale del bot.
Tutti i parametri di rischio, filtri e API sono definiti qui.
Le chiavi segrete vanno SOLO nel file .env, mai nel codice.

DATA PLANE (riscritto per l'alta frequenza)
-------------------------------------------
Il bot legge tutto ciò che gli serve da Jupiter Tokens API v2, che in UNA
richiesta (~230 ms misurati) restituisce per ogni token: prezzo, market cap,
liquidità, statistiche 5m/1h/24h (volume organico incluso), audit on-chain
(mint/freeze authority, concentrazione top holder, quanti token ha già
lanciato lo stesso dev) e i link social. Prima servivano 3 chiamate su 2
fornitori diversi per gli stessi dati, ed erano meno completi.

Il monitoraggio delle posizioni usa Jupiter Price v3 in batch: un'unica
richiesta (~150 ms misurati) copre tutte le posizioni aperte, quindi la
cadenza del loop di rischio non dipende dal numero di posizioni.

I valori di soglia in FilterConfig non sono scelti a intuito: derivano dalla
distribuzione misurata su 105 token reali dei tre feed Jupiter. I percentili
di riferimento sono annotati accanto a ogni parametro.
"""

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("config")

# Parametri modificati a runtime dal supervisore (agent.py) e ricaricati
# all'avvio: senza questo file le sue ottimizzazioni sparivano ad ogni
# riavvio del processo mentre i report continuavano a dichiararle attive.
FILE_PARAMETRI_RUNTIME = "parametri_runtime.json"


@dataclass
class APIConfig:
    # RPC Solana (Helius consigliato: piano free = 100k richieste/giorno)
    helius_api_key: str = os.getenv("HELIUS_API_KEY", "")
    rpc_url: str = field(init=False)
    ws_url: str = field(init=False)

    # Wallet (chiave privata in base58 — TENERE SOLO IN .env)
    wallet_private_key: str = os.getenv("WALLET_PRIVATE_KEY", "")
    # Chiave pubblica attesa dello stesso wallet (non segreta): usata solo per
    # verificare all'avvio che WALLET_PRIVATE_KEY corrisponda al wallet giusto.
    wallet_public_key: str = os.getenv("WALLET_PUBLIC_KEY", "")

    # Telegram — notifiche + comandi PARTI/PAUSA/STOP (opzionale: se mancano
    # queste due, l'integrazione resta disattivata senza rompere il bot).
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Jito Block Engine (ingresso prioritario). NB: sendBundle vuole
    # esplicitamente {"encoding": "base64"}, altrimenti assume base58 e
    # rifiuta il bundle — vedi executor._invia_via_jito.
    jito_block_engine_url: str = "https://mainnet.block-engine.jito.wtf/api/v1/bundles"
    jito_tip_lamports: int = 200_000

    # ---- JUPITER ----
    # L'host storico quote-api.jup.ag/v6 è stato spento e oggi non risolve
    # nemmeno in DNS: qualunque quote su quell'endpoint fallisce, e con essa
    # ogni acquisto E ogni vendita di emergenza. lite-api è il piano gratuito
    # senza API key; chi ha una chiave Jupiter può passare ad api.jup.ag
    # impostando JUPITER_BASE_URL nel .env.
    jupiter_base: str = os.getenv("JUPITER_BASE_URL", "https://lite-api.jup.ag")
    jupiter_quote_url: str = field(init=False)
    jupiter_swap_url: str = field(init=False)
    jupiter_price_url: str = field(init=False)
    jupiter_tokens_base: str = field(init=False)

    # Endpoint di riserva (usati solo se Jupiter non risponde)
    dexscreener_url: str = "https://api.dexscreener.com"
    rugcheck_url: str = "https://api.rugcheck.xyz/v1"

    def __post_init__(self):
        self.rpc_url = f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
        self.ws_url = f"wss://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
        b = self.jupiter_base.rstrip("/")
        self.jupiter_quote_url = f"{b}/swap/v1/quote"
        self.jupiter_swap_url = f"{b}/swap/v1/swap"
        self.jupiter_price_url = f"{b}/price/v3"
        self.jupiter_tokens_base = f"{b}/tokens/v2"


@dataclass
class RiskConfig:
    """
    Parametri calibrati per un capitale di partenza di ~100 EUR.
    NOTA: con capitale così piccolo le fee pesano molto. Ogni giro completo
    costa ~1-3% tra fee di rete, priority fee, tip Jito e slippage: serve
    circa un +3% solo per andare in pari.
    """
    capitale_iniziale_eur: float = 100.0

    # Position sizing
    max_posizione_pct: float = 0.15          # ~15 EUR a trade
    max_posizioni_aperte: int = 4            # alzato da 3: più rotazione, più campione

    # ---- USCITE ----
    # Il ladder precedente (x5/x10/x50) era in contraddizione con il trailing:
    # con un trailing del 20% nella fascia x1-x10, un token che fa x6 e
    # ritraccia del 20% viene chiuso interamente a x4.8 e il tier x50 non può
    # essere raggiunto per costruzione. Il ladder ora recupera il capitale
    # presto (x1.8 vende il 35% → oltre metà dell'investito è già rientrato)
    # e lascia correre il resto con un trailing largo.
    tp_ladder: tuple = ((1.8, 0.35), (3.0, 0.25), (6.0, 0.20), (15.0, 0.10))
    stop_loss_pct: float = -0.22             # -22%: taglia prima, ruota di più

    # Trailing DINAMICO per fascia di multiplo. Allargato: una memecoin
    # ritraccia del 20% di routine, il trailing precedente uccideva i runner
    # prima che corressero.
    trailing_bands: tuple = ((3.0, 0.30), (10.0, 0.35), (float("inf"), 0.42))
    # Volatility stop: si applica SOLO al crollo (variazione 5m negativa) e
    # solo se siamo già in profitto oltre volatility_stop_min_pnl. Applicarlo
    # al valore assoluto, come faceva prima, lo metteva in conflitto diretto
    # col momentum d'ingresso: un token comprato a +91% in 5 minuti veniva
    # rivenduto immediatamente perché quella stessa salita superava la soglia.
    volatility_stop_change5m: float = 55.0
    volatility_stop_min_pnl: float = 0.15
    # Nessuna uscita non-urgente nei primi secondi dopo l'ingresso: i dati di
    # mercato riflettono ancora il movimento su cui si è entrati. Lo stop loss
    # è escluso da questa grazia e resta sempre attivo.
    grazia_ingresso_sec: float = 40.0
    # Il trailing si attiva solo dopo che la posizione ha superato questo
    # multiplo: sotto, comanda lo stop loss. Evita di uscire per trailing su
    # un rumore di pochi punti percentuali subito dopo l'ingresso.
    trailing_attivo_da_multiplo: float = 1.25

    # Kelly sizing: size = max_posizione_pct * (score_composito / 100)
    kelly_sizing: bool = True
    min_posizione_pct: float = 0.06

    # ---- CLUSTERING D'INGRESSO ----
    # Era 7 MINUTI di attesa prima di comprare: su una memecoin è un'era
    # geologica, il segnale è morto o il pump è già finito. Ridotto a 45
    # secondi, che basta a confrontare i candidati arrivati nello stesso
    # intorno temporale senza buttare via il timing.
    cluster_buffer_sec: int = 45
    cluster_top_n: int = 2

    # ---- USCITE A TEMPO ----
    # Accorciate: la rotazione veloce è il punto di un bot ad alta frequenza.
    # Un token che non si muove entro 25 minuti occupa uno slot che potrebbe
    # servire a un segnale vivo.
    momentum_check_minuti: int = 25
    momentum_check_min_pnl: float = 0.08
    max_holding_ore: int = 6

    # ---- VENDITE ----
    # tranches_per_vendita era 3 con un timeout di 15 minuti PER TRANCHE:
    # fino a 45 minuti di loop principale congelato (nessuna scansione,
    # nessun controllo del saldo, altre posizioni scoperte). Ora le vendite
    # girano in task separati e non bloccano più il loop, ma restano a 1
    # tranche: una posizione da 15 EUR non muove un pool da 20k, e ogni
    # tranche in più è solo un'altra fee.
    tranches_per_vendita: int = 1
    calm_soglia_vendita: float = 45.0
    calm_check_sec: int = 5
    calm_timeout_sec: int = 45                # era 15 MINUTI per tranche
    max_slippage_tranche_pct: float = 8.0
    # Uno stop loss non aspetta mai il market calm: esce subito e secco.
    calm_ignora_su_stop_loss: bool = True

    # Circuit breaker giornaliero
    max_perdita_giornaliera_pct: float = 0.20

    # ---- STOP DI SICUREZZA SU SALDO REALE (backstop indipendente) ----
    # Confronta il valore TOTALE del wallet (SOL + valore corrente delle
    # posizioni aperte) con questa frazione del capitale iniziale. La versione
    # precedente confrontava il solo saldo SOL col capitale totale: con
    # posizioni aperte fino al 60% del capitale non è in SOL, e il controllo
    # generava falsi positivi che imponevano una pausa INDEFINITA con
    # posizioni aperte e non più protette.
    floor_sicurezza_pct: float = 0.35

    # ---- ESECUZIONE ----
    # dynamicSlippage lascia a Jupiter il calcolo dello slippage per rotta:
    # il 3% fisso precedente faceva fallire in preflight gran parte degli
    # swap su memecoin fresche.
    usa_dynamic_slippage: bool = True
    slippage_bps: int = 900                  # usato solo se dynamic è off
    max_slippage_bps_dinamico: int = 1500    # tetto passato a Jupiter

    # Priority fee alzata: in alta frequenza la tx deve entrare nel blocco
    # successivo, non in quello dopo.
    priority_fee_microlamports: int = 500_000
    usa_jito: bool = True

    # Conferma on-chain: poll più rapido e finestra più corta. Se scade, il
    # bot NON assume il fallimento: rilegge il saldo token reale (vedi
    # executor._recupera_posizione_da_saldo) prima di rinunciare.
    timeout_conferma_sec: float = 30.0
    poll_conferma_sec: float = 0.6


@dataclass
class FilterConfig:
    """
    Filtri anti-rug. Un token deve passarli TUTTI prima dello scoring.

    I dati arrivano dal blocco `audit` di Jupiter Tokens v2, che espone gli
    stessi controlli on-chain per cui prima serviva una chiamata a RugCheck
    per candidato — ma senza il costo in latenza e senza il bug di scala che
    rendeva quel gate impossibile da superare.

    I percentili annotati sono misurati su 105 token reali dei feed Jupiter.
    """
    # ================= LA MANOPOLA DELLA FREQUENZA =================
    # Questi due parametri, più min_eta_pool_minuti, decidono da soli quanti
    # trade fa il bot. Misurato su 470 candidati reali: con liquidità minima a
    # 6.000 e età minima a 4 minuti, il 35% dei candidati moriva su "pool
    # troppo giovane" e il 14% su "liquidità", e NIENTE arrivava allo scoring.
    # Il motivo è che 6.000 era esattamente la mediana della popolazione
    # bersaglio (mediana misurata: $0 sotto i 5 minuti, $5.500 tra 5 e 30
    # minuti, $15.000 oltre i 30): tagliare sulla mediana significa scartare
    # metà di tutto per definizione.
    #
    # ATTENZIONE, QUESTO È UN COMPROMESSO DI RISCHIO, NON UN'OTTIMIZZAZIONE:
    # abbassare questi valori aumenta il numero di trade E l'esposizione ai
    # rug, perché i token più giovani e più sottili sono esattamente quelli
    # dove i rug avvengono. La protezione vera non sono queste soglie ma i
    # filtri di audit (mint/freeze authority revocate, max_dev_mints), che
    # restano invariati e che da soli scartano il 29% dei candidati.
    #
    # Per MENO trade e meno rischio: liquidità 10.000, età 6 minuti.
    # Per PIÙ trade e più rischio:   liquidità 3.000, età 2 minuti.
    min_liquidity_usd: float = 4_000
    min_volume_5m_usd: float = 2_500

    # Età del pool. La finestra è molto più larga di prima (era 5-60 min) non
    # per essere permissivi, ma perché ora un token scartato per età TORNA nel
    # ciclo successivo invece di essere buttato per sempre (vedi scanner.py).
    max_eta_pool_minuti: int = 360
    # 3 minuti è il compromesso: sotto i 2 minuti la quota di rug istantanei
    # sale molto e nemmeno l'audit di Jupiter è ancora popolato (misurato:
    # topHoldersPercentage assente su 24 token su 130, tutti freschissimi).
    # Vedi la nota sulla manopola della frequenza sopra.
    min_eta_pool_minuti: int = 3

    # Banda di market cap. La mediana misurata è $152k: la banda precedente
    # (150k-1.5M) tagliava fuori l'intera metà inferiore della popolazione.
    min_market_cap_usd: float = 20_000
    max_market_cap_usd: float = 5_000_000
    # Sanity: liquidità almeno il 4% del mcap (mcap gonfiato = trappola)
    min_liq_su_mcap: float = 0.04

    # ---- AUDIT ON-CHAIN (da Jupiter audit) ----
    richiedi_mint_revocato: bool = True
    richiedi_freeze_revocato: bool = True
    # Concentrazione top holder. Misurato: mediana 23.5%, p75 36.8%.
    # Il vecchio 35% era applicato ai topHolders di RugCheck, che includono
    # il pool stesso: 8 token su 12 risultavano al 41-100% e venivano
    # scartati anche quando la distribuzione reale era sana.
    max_top_holders_pct: float = 40.0
    # Quota in mano al dev (misurato: mediana 2.4%, p75 3.4%)
    max_dev_balance_pct: float = 12.0
    # Quanti token ha già creato lo stesso dev. Misurato: mediana 1, p75 133,
    # max 148.361. Oltre qualche decina è una fabbrica di lanci seriali, non
    # un progetto: è uno dei segnali anti-rug più forti del feed.
    max_dev_mints: int = 50

    # ---- QUALITÀ DEL FLUSSO ----
    # organicScore è la stima Jupiter di quanta parte del volume è reale
    # invece di wash trading (misurato: mediana 52.7 sui token maturi).
    # Sostituisce l'euristica locale vol/liq, che non distingueva hype forte
    # da volume finto.
    min_organic_score: float = 30.0
    # ATTENZIONE: Jupiter calcola organicScore solo dopo che il token ha una
    # storia. Misurato: è 0 per il 100% dei token sotto i 30 minuti di vita,
    # e valorizzato per il 93% di quelli oltre. Applicare il gate sotto questa
    # età scarterebbe ogni token fresco per un dato che semplicemente non
    # esiste ancora, non perché il volume sia finto.
    organic_gate_da_eta_minuti: float = 30.0
    # Rapporto minimo tra volume organico e volume totale sulla finestra 1h,
    # applicato solo quando il dato è presente.
    min_quota_volume_organico: float = 0.02

    min_buy_sell_ratio: float = 0.85

    # Tetto sulla variazione 5m accettata in ingresso. Nei feed live si vedono
    # regolarmente token a +1500% o +4000% in 5 minuti: comprare lì è comprare
    # il tetto del pump, e il ritracciamento arriva prima che lo stop loss
    # possa fare qualcosa di utile. La rivalidazione pre-acquisto copre la
    # deriva tra scoring e acquisto, non un pump già avvenuto prima di vederlo.
    #
    # NB sulla coerenza tra i due estremi: questo valore va tenuto sopra
    # RiskConfig.volatility_stop_change5m ma nello stesso ordine di grandezza.
    # Se il tetto d'ingresso fosse molto più alto della soglia del volatility
    # stop si comprerebbero sistematicamente token destinati a essere chiusi
    # al primo ritracciamento.
    max_price_change_5m_ingresso: float = 120.0

    # ---- SENTIMENT SOCIAL ----
    # Scala 0-100 sui campi social di Jupiter (twitter/telegram/website/
    # isVerified/icon). Il gate resta sulla presenza minima: senza ricerca
    # web non si distingue hype organico da shill coordinato, e inventare
    # quel segnale sarebbe peggio che dichiararne l'assenza.
    min_sentiment_score: int = 30

    # ---- MOMENTUM D'INGRESSO ----
    min_momentum_score: float = 42.0

    # ---- DEEP CHECK PRE-ACQUISTO ----
    # RugCheck non è più nel percorso veloce (costava una chiamata di rete per
    # candidato, cioè latenza sul segnale). Viene interrogato una sola volta,
    # sul candidato che sta per essere comprato davvero.
    # NB SULLA SCALA: RugCheck restituisce score_normalised 0-100 dove BASSO
    # significa SICURO — è un punteggio di RISCHIO. La versione precedente
    # richiedeva `score >= 500` credendola una scala 0-5000 di qualità: nessun
    # token può superare 100, quindi quel gate rifiutava il 100% dei token e
    # il bot non poteva comprare nulla. Verificato su 12 token reali: i più
    # sani segnano 1, i rischiosi 31 e 53.
    usa_rugcheck_deep_check: bool = True
    max_rugcheck_score: int = 40             # scartare se score_normalised >
    rugcheck_scarta_se_non_disponibile: bool = False


@dataclass
class BotConfig:
    api: APIConfig = field(default_factory=APIConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)

    # Modalità: "paper" = simulazione senza soldi veri, "live" = trading reale.
    # DEFAULT PAPER. Il default precedente era "live": con i blocchi che
    # impedivano ogni acquisto era innocuo, ma ora che il bot compra davvero
    # un primo avvio non voluto in live opererebbe sul saldo reale del wallet
    # senza nessuna validazione a monte. Per operare con soldi veri serve un
    # BOT_MODE=live esplicito nel .env.
    mode: str = os.getenv("BOT_MODE", "paper")

    # ---- CADENZE E BUDGET DI RICHIESTE ----
    # Prima esisteva un solo loop a 20s che faceva discovery E monitoraggio, e
    # una vendita a tranche poteva bloccarlo per 45 minuti. Ora sono loop
    # indipendenti: il rischio non aspetta la scansione e viceversa.
    #
    # ATTENZIONE, VINCOLO MISURATO: il piano gratuito Jupiter concede 60
    # richieste al minuto. Con discovery a 4s su 3 feed (45/min) più
    # monitoraggio prezzi a 1.5s sullo stesso host (40/min) si arriva a ~85/min
    # e dopo circa 90 secondi TUTTI i feed rispondono 429 restituendo zero
    # elementi — un bot che non compra più nulla senza un solo errore a log.
    # Verificato sul campo in questo repo.
    #
    # Le cadenze qui sotto stanno dentro il budget dividendo il carico su due
    # fornitori con limiti separati: Jupiter per la discovery e gli swap,
    # DexScreener per il monitoraggio dei prezzi. Chi vuole andare più veloce
    # deve prendere una API key Jupiter e impostare
    # JUPITER_BASE_URL=https://api.jup.ag nel .env: è l'unico modo pulito di
    # alzare il tetto, e allora questi intervalli si possono ridurre.
    scan_interval_sec: float = 3.0            # ciclo del loop discovery
    monitor_interval_sec: float = 2.0         # SL/TP/trailing (su DexScreener)
    prezzo_sol_refresh_sec: float = 45.0
    saldo_refresh_sec: float = 20.0

    # Richieste al minuto concesse per fornitore (vedi ratelimit.py).
    # Tenute sotto il tetto reale per lasciare margine a quote e swap, che
    # arrivano a raffica quando si apre o chiude una posizione.
    limite_jupiter_al_minuto: int = 45
    limite_dexscreener_al_minuto: int = 55

    # Cadenza PER FEED, proporzionata al ricambio reale misurato in 75s:
    # recent 30 nuovi su 30 (ricambio totale), trending 15 su 50, traded 6 su
    # 50. Interrogarli tutti alla stessa frequenza sprecava budget sui due
    # feed lenti sottraendolo a quello che cambia davvero.
    # Consumo misurato con questi valori: ~15 richieste/minuto verso Jupiter
    # contro un budget di 45, quindi resta margine ampio per quote e swap.
    # Non serve andare oltre: il vincolo non è la frequenza di polling ma il
    # ricambio dei feed, misurato in ~41 mint nuovi al minuto. Interrogare
    # `recent` ogni secondo non farebbe comparire token che non esistono.
    feed_recent_ogni_sec: float = 4.0         # ~15 richieste/min
    feed_trending_ogni_sec: float = 15.0      # ~4 richieste/min
    feed_traded_ogni_sec: float = 40.0        # ~1.5 richieste/min

    max_candidati_paralleli: int = 12        # era 5

    file_controllo: str = "control.json"

    # Analisi quantitativa locale come gate finale prima dell'acquisto.
    usa_analisi_locale: bool = True
    # Soglia minima score locale (0-100) per aprire una posizione.
    # Rinominato da min_claude_score: non c'è più nessuna AI nel percorso.
    min_score_locale: int = 58

    # Log periodico dell'imbuto degli scarti (0 = disattivato). È lo strumento
    # che rende tarabile il bot: senza sapere QUALE stadio uccide i candidati,
    # ogni modifica alle soglie è un tentativo alla cieca.
    log_imbuto_ogni_sec: float = 120.0

    # ---- PERSISTENZA DEI PARAMETRI DEL SUPERVISORE ----

    def carica_parametri_runtime(self) -> dict:
        """Riapplica le modifiche fatte dal supervisore nelle sessioni
        precedenti. Senza questo, `agent.py` faceva setattr su oggetti in
        memoria e ad ogni riavvio (Railway ne fa di routine) i parametri
        tornavano ai default mentre report_agente.md continuava a
        dichiarare le modifiche attive."""
        try:
            with open(FILE_PARAMETRI_RUNTIME) as f:
                salvati = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        applicati = {}
        for nome, valore in (salvati.get("parametri") or {}).items():
            for target in (self.risk, self.filters, self):
                if hasattr(target, nome):
                    setattr(target, nome, valore)
                    applicati[nome] = valore
                    break
        if applicati:
            log.info("Parametri runtime ripristinati da %s: %s", FILE_PARAMETRI_RUNTIME, applicati)
        return applicati

    def salva_parametri_runtime(self, parametri: dict):
        """Unisce `parametri` a quelli già su disco e riscrive il file."""
        try:
            with open(FILE_PARAMETRI_RUNTIME) as f:
                correnti = (json.load(f).get("parametri") or {})
        except (FileNotFoundError, json.JSONDecodeError):
            correnti = {}
        correnti.update(parametri)
        import time
        with open(FILE_PARAMETRI_RUNTIME, "w") as f:
            json.dump({"aggiornato_ts": time.time(), "parametri": correnti}, f, indent=2)

    def snapshot(self) -> dict:
        """Configurazione effettiva, senza segreti — per log e dashboard."""
        api = {k: v for k, v in asdict(self.api).items()
               if not any(s in k for s in ("private_key", "api_key", "token", "chat_id"))}
        return {"mode": self.mode, "api": api, "risk": asdict(self.risk),
                "filters": asdict(self.filters)}


CONFIG = BotConfig()
CONFIG.carica_parametri_runtime()
