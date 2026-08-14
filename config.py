"""
config.py — Configurazione centrale del bot.
Tutti i parametri di rischio, filtri e API sono definiti qui.
Le chiavi segrete vanno SOLO nel file .env, mai nel codice.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class APIConfig:
    # RPC Solana (Helius consigliato: piano free = 100k richieste/giorno)
    helius_api_key: str = os.getenv("HELIUS_API_KEY", "")
    rpc_url: str = field(init=False)
    ws_url: str = field(init=False)

    # Wallet (chiave privata in base58 — TENERE SOLO IN .env)
    wallet_private_key: str = os.getenv("WALLET_PRIVATE_KEY", "")

    # Telegram — notifiche + comandi PARTI/PAUSA/STOP (opzionale: se mancano
    # queste due, l'integrazione resta disattivata senza rompere il bot).
    # Setup: vedi l'intestazione di telegram_bot.py.
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Jito Block Engine (ingresso prioritario, alternativa reale a Photon
    # dato che Photon non espone API per bot — vedi commento in executor.py)
    jito_block_engine_url: str = "https://mainnet.block-engine.jito.wtf/api/v1/bundles"
    jito_tip_lamports: int = 100_000  # tip ai validator Jito, oltre alla priority fee

    # Endpoint pubblici
    jupiter_quote_url: str = "https://quote-api.jup.ag/v6/quote"
    jupiter_swap_url: str = "https://quote-api.jup.ag/v6/swap"
    dexscreener_url: str = "https://api.dexscreener.com"
    rugcheck_url: str = "https://api.rugcheck.xyz/v1"

    def __post_init__(self):
        self.rpc_url = f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
        self.ws_url = f"wss://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"


@dataclass
class RiskConfig:
    """
    Parametri calibrati per un capitale di partenza di ~100 EUR.
    NOTA: con capitale così piccolo le fee pesano molto.
    Ogni trade su Solana costa ~0.5-2% tra fee, priority fee e slippage.
    """
    capitale_iniziale_eur: float = 100.0

    # Position sizing: max 15% del capitale per singolo trade
    max_posizione_pct: float = 0.15          # ~15 EUR a trade
    max_posizioni_aperte: int = 3            # mai più di 3 token in portafoglio

    # Uscite — LADDER x5 / x10 / x50
    # Ogni tier: (multiplo del prezzo di entrata, frazione della POSIZIONE ORIGINALE da vendere)
    # A x5 vendi il 40% (recuperi 2x l'investimento iniziale → da qui giochi "gratis")
    # A x10 vendi il 30% | a x50 vendi il 25% | il 5% resta come moonbag col trailing
    tp_ladder: tuple = ((5.0, 0.40), (10.0, 0.30), (50.0, 0.25))
    stop_loss_pct: float = -0.30             # -30% → vendi tutto

    # Trailing stop DINAMICO per fascia di multiplo:
    # x1-x10: 20% (proteggi il guadagno) | x10-x30: 15% (runner precario)
    # x30+: 25% (moonbag: lascia respirare il raro scenario estremo)
    trailing_bands: tuple = ((10.0, 0.20), (30.0, 0.15), (float("inf"), 0.25))
    # Volatility stop: in profitto e |var 5m| > 60% → panico, esci subito
    volatility_stop_change5m: float = 60.0

    # Kelly sizing: size = max_posizione_pct * (score_composito / 100)
    # con pavimento per non aprire posizioni-briciola mangiate dalle fee
    kelly_sizing: bool = True
    min_posizione_pct: float = 0.05

    # Clustering d'ingresso: bufferizza i candidati e compra i migliori del lotto
    cluster_buffer_min: int = 7              # finestra di raccolta (minuti)
    cluster_top_n: int = 2                   # quanti comprare per cluster

    # Time-based exit a due stadi (con target x5-x50 servono tempi più lunghi)
    momentum_check_minuti: int = 120         # dopo 2h: se PnL < +20% → esci (token morto)
    momentum_check_min_pnl: float = 0.20
    max_holding_ore: int = 24                # dopo 24h → esci comunque

    # Uscite parzializzate (tranche) con market calm
    tranches_per_vendita: int = 3            # ogni vendita divisa in 3 ordini
    calm_soglia_vendita: float = 60.0        # vendi la tranche solo se calm > 60
    calm_check_sec: int = 30                 # ricontrolla il calm ogni 30s
    calm_timeout_min: int = 15               # dopo 15 min vendi comunque (no blocchi)
    max_slippage_tranche_pct: float = 5.0    # se impatto stimato > 5% → attendi

    # Circuit breaker giornaliero
    max_perdita_giornaliera_pct: float = 0.20  # -20% in un giorno → bot in pausa 24h

    # ---- STOP DI SICUREZZA SU SALDO REALE (backstop indipendente) ----
    # A differenza del circuit breaker sopra (basato sulla contabilità INTERNA,
    # che può sbagliare in caso di bug o vendite parziali), questo controllo
    # legge il saldo SOL REALE del wallet on-chain ad ogni ciclo. Se scende
    # sotto questa percentuale del capitale iniziale, il bot si ferma da solo
    # e resta fermo finché l'utente non lo riavvia esplicitamente con
    # "python control.py start" — è l'ultima linea di difesa contro qualunque
    # bug di contabilità o perdita più rapida del previsto.
    floor_sicurezza_pct: float = 0.30

    # Slippage massimo accettato (in basis points, 300 = 3%)
    slippage_bps: int = 300

    # Priority fee (microlamports) — necessaria per essere veloci
    priority_fee_microlamports: int = 100_000

    # Ingresso via Jito bundle (bypassa la coda pubblica RPC): consigliato per
    # gli acquisti dove i primi secondi contano; fallback automatico su RPC
    # normale se Jito non risponde. Non richiede alcuna chiave aggiuntiva.
    usa_jito: bool = True


@dataclass
class FilterConfig:
    """Filtri anti-rug: un token deve passarli TUTTI prima dell'analisi AI."""
    min_liquidity_usd: float = 20_000        # liquidità minima nel pool
    min_volume_5m_usd: float = 5_000         # volume minimo negli ultimi 5 min
    max_eta_pool_minuti: int = 60            # solo pool nati da meno di 1h
    min_eta_pool_minuti: int = 5             # ma non nei primi 5 min (troppi rug)
    max_holder_top10_pct: float = 0.35       # top 10 holder < 35% della supply
    richiedi_mint_revocato: bool = True      # mint authority deve essere revocata
    richiedi_freeze_revocato: bool = True    # freeze authority deve essere revocata
    richiedi_lp_bloccata: bool = True        # LP burned o locked
    min_rugcheck_score: int = 500            # score minimo su RugCheck (0-5000)

    # ---- ENTRY BAND DI MARKET CAP (razionale nel commento) ----
    # < 150k: fase bonding-curve/lancio, dominata da sniper bot e rug istantanei.
    # 150k–1.5M: "sweet spot" — sopravvissuto al lancio, LP formata, ma con
    #   spazio matematico per fare x5-x50 (a 1.5M un x50 = 75M: raro ma successo;
    #   a 20M un x50 = 1B: praticamente mai).
    # > 1.5M: il grosso del multiplo è già stato fatto da altri.
    min_market_cap_usd: float = 150_000
    max_market_cap_usd: float = 1_500_000
    # Sanity: la liquidità deve essere almeno l'8% del mcap (mcap gonfiato = trappola)
    min_liq_su_mcap: float = 0.08

    # ---- SENTIMENT SOCIAL ----
    # Score 0-100 puramente strutturale (twitter/telegram/sito/immagine/boost su
    # DexScreener): senza AI+web search non si distingue più hype organico da
    # shill coordinato, quindi il gate resta solo sulla presenza social minima.
    min_sentiment_score: int = 55            # gate d'ingresso sul sentiment

    # ---- MOMENTUM D'INGRESSO ----
    min_momentum_score: float = 50.0         # sotto: token buono ma timing sbagliato


@dataclass
class BotConfig:
    api: APIConfig = field(default_factory=APIConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)

    # Modalità: "paper" = simulazione senza soldi veri, "live" = trading reale
    # INIZIA SEMPRE IN PAPER MODE per almeno 2 settimane
    mode: str = os.getenv("BOT_MODE", "paper")

    # Intervallo di scansione nuovi pool (secondi)
    scan_interval_sec: int = 20

    # Quanti candidati valutare IN PARALLELO ad ogni ciclo (filtri + sentiment +
    # Claude). Con valutazione sequenziale, il tempo reale di un ciclo può
    # superare ampiamente scan_interval_sec quando arrivano molti candidati
    # insieme (il sentiment/regime usano web_search, timeout fino a 90s).
    max_candidati_paralleli: int = 5

    # File di controllo manuale (start/pausa/stop), letto ad ogni ciclo.
    file_controllo: str = "control.json"

    # Usa l'analisi quantitativa locale (local_analyzer.py) come gate finale
    # prima dell'acquisto. Nessuna chiamata AI esterna, nessun costo, nessuna
    # dipendenza da ANTHROPIC_API_KEY.
    usa_analisi_locale: bool = True

    # Soglia minima score (0-100, formula locale) per aprire una posizione
    min_claude_score: int = 70


CONFIG = BotConfig()
