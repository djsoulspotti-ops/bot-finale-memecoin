"""
scanner.py — Rileva token Solana candidati, tramite Jupiter Tokens API v2.

PERCHÉ NON PIÙ DEXSCREENER token-profiles
-----------------------------------------
La versione precedente interrogava `/token-profiles/latest/v1`, che NON
restituisce i pool nuovi: restituisce i token che hanno appena creato o
aggiornato una scheda a pagamento su DexScreener. Popolazione sbagliata e
minuscola — 30 elementi in tutto, 21 su Solana, con età misurate da 6 minuti
a 496.384 minuti (circa un anno). Poi servivano altre due chiamate per
arricchire i dati (pairs + RugCheck) prima di poter decidere qualcosa.

Ora i tre feed Jupiter uniti danno ~105 token unici per ciclo, di cui l'80%
da pump.fun, e ogni token arriva GIÀ completo di prezzo, liquidità, market
cap, statistiche 5m/1h/24h con volume organico separato, audit on-chain e
link social. Una richiesta per feed, ~230 ms misurati, nessuna API key.

PERCHÉ NON logsSubscribe
------------------------
La strada "vera" per l'alta frequenza sarebbe intercettare le creazioni di
pool via WebSocket. Misurato sull'RPC pubblico: sottoscrivendo i programmi
Pump.fun / PumpSwap / Raydium arrivano rispettivamente 372, 1178 e 38
transazioni al SECONDO, perché il filtro `mentions` cattura ogni swap che
tocca il programma e non è filtrabile per istruzione. Le creazioni sono raro
rumore di fondo in quel torrente: andrebbe ingerito tutto per poi scartare
il 99,9%, bruciando la quota Helius in poche ore. Il feed `recent` di Jupiter
espone gli stessi lanci (misurato: token presenti 4 secondi dopo la
creazione) al costo di una richiesta.

WATCHLIST INVECE DI "VISTI"
---------------------------
Il vecchio `self.visti` marcava ogni mint alla PRIMA apparizione, prima di
qualunque filtro, e non lo rivalutava mai più. Con una finestra d'ingresso di
5-60 minuti, un token intercettato a 2 minuti di vita veniva scartato per
"pool troppo giovane" e non tornava mai a 10 minuti, quando sarebbe stato
idoneo: ogni token aveva UNA sola occasione, in un istante casuale.
Ora un mint viene escluso solo per un TTL breve (`ttl_ricontrollo_sec`), così
torna nel ciclo successivo, e viene bloccato definitivamente solo dopo un
esito definitivo (comprato, o scartato per un motivo che non può cambiare).
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiohttp

from config import CONFIG
from ratelimit import REGISTRO, richiesta_json

log = logging.getLogger("scanner")


def _eta_minuti_da_iso(iso: str | None) -> float:
    """Età in minuti da un timestamp ISO8601. Ritorna un valore enorme se
    il dato manca, così i filtri di età lo scartano invece di trattarlo
    come appena nato."""
    if not iso:
        return 1e9
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except (ValueError, TypeError):
        return 1e9


@dataclass
class TokenCandidate:
    mint: str
    symbol: str
    name: str
    dex: str                     # launchpad: pump.fun / letsbonk.fun / ...
    liquidity_usd: float
    volume_5m_usd: float
    price_usd: float
    market_cap_usd: float
    eta_minuti: float

    # Momentum 5m
    price_change_5m: float = 0.0
    buys_5m: int = 0
    sells_5m: int = 0
    traders_5m: int = 0
    net_buyers_5m: int = 0
    volume_change_5m: float = 0.0
    liquidity_change_5m: float = 0.0

    # Qualità del flusso
    organic_score: float = 0.0
    organic_label: str = "low"
    quota_volume_organico: float | None = None

    # Audit on-chain (blocco `audit` di Jupiter)
    mint_revocato: bool = False
    freeze_revocato: bool = False
    top_holders_pct: float | None = None
    dev_balance_pct: float | None = None
    dev_mints: int | None = None

    # Social
    ha_twitter: bool = False
    ha_telegram: bool = False
    ha_sito: bool = False
    verificato: bool = False
    ha_icona: bool = False

    holder_count: int | None = None
    holder_change_1h: float | None = None
    decimals: int = 6
    feed: tuple = ()
    raw: dict = field(default_factory=dict)

    @property
    def buy_sell_ratio(self) -> float:
        return self.buys_5m / max(self.sells_5m, 1)

    @property
    def liq_su_mcap(self) -> float:
        return self.liquidity_usd / self.market_cap_usd if self.market_cap_usd > 0 else 0.0


def _parse_token(t: dict, feed: str) -> TokenCandidate | None:
    """Converte un token Jupiter v2 in TokenCandidate. Ritorna None se mancano
    i campi indispensabili per poterlo valutare."""
    mint = t.get("id")
    prezzo = t.get("usdPrice")
    if not mint or not prezzo:
        return None

    audit = t.get("audit") or {}
    s5 = t.get("stats5m") or {}
    s1h = t.get("stats1h") or {}

    vol_5m = (s5.get("buyVolume") or 0.0) + (s5.get("sellVolume") or 0.0)

    # Quota di volume organico: disponibile solo sulla finestra 1h, e solo per
    # i token che Jupiter ha già classificato. Se manca resta None e il filtro
    # relativo viene saltato invece di dare per buono un valore inventato.
    vol_1h = (s1h.get("buyVolume") or 0.0) + (s1h.get("sellVolume") or 0.0)
    org_1h = (s1h.get("buyOrganicVolume") or 0.0) + (s1h.get("sellOrganicVolume") or 0.0)
    quota_org = (org_1h / vol_1h) if vol_1h > 0 and org_1h > 0 else None

    # L'età si prende dal primo pool, non dalla creazione del mint: un token
    # può essere mintato molto prima di avere un mercato.
    creato = (t.get("firstPool") or {}).get("createdAt") or t.get("createdAt")

    # Jupiter espone la revoca in due modi a seconda del feed: il booleano
    # audit.*Disabled (feed recent) o il campo authority a livello alto, che
    # è None quando la authority è stata revocata (feed trending/traded).
    mint_rev = audit.get("mintAuthorityDisabled")
    if mint_rev is None:
        mint_rev = t.get("mintAuthority") is None
    freeze_rev = audit.get("freezeAuthorityDisabled")
    if freeze_rev is None:
        freeze_rev = t.get("freezeAuthority") is None

    return TokenCandidate(
        mint=mint,
        symbol=(t.get("symbol") or "?")[:16],
        name=t.get("name") or "?",
        dex=t.get("launchpad") or "n/d",
        liquidity_usd=float(t.get("liquidity") or 0.0),
        volume_5m_usd=float(vol_5m),
        price_usd=float(prezzo),
        market_cap_usd=float(t.get("mcap") or t.get("fdv") or 0.0),
        eta_minuti=_eta_minuti_da_iso(creato),
        price_change_5m=float(s5.get("priceChange") or 0.0),
        buys_5m=int(s5.get("numBuys") or 0),
        sells_5m=int(s5.get("numSells") or 0),
        traders_5m=int(s5.get("numTraders") or 0),
        net_buyers_5m=int(s5.get("numNetBuyers") or 0),
        volume_change_5m=float(s5.get("volumeChange") or 0.0),
        liquidity_change_5m=float(s5.get("liquidityChange") or 0.0),
        organic_score=float(t.get("organicScore") or 0.0),
        organic_label=t.get("organicScoreLabel") or "low",
        quota_volume_organico=quota_org,
        mint_revocato=bool(mint_rev),
        freeze_revocato=bool(freeze_rev),
        top_holders_pct=audit.get("topHoldersPercentage"),
        dev_balance_pct=audit.get("devBalancePercentage"),
        dev_mints=audit.get("devMints"),
        ha_twitter=bool(t.get("twitter")),
        ha_telegram=bool(t.get("telegram")),
        ha_sito=bool(t.get("website")),
        verificato=bool(t.get("isVerified")),
        ha_icona=bool(t.get("icon")),
        holder_count=t.get("holderCount"),
        holder_change_1h=s1h.get("holderChange"),
        decimals=int(t.get("decimals") or 6),
        feed=(feed,),
        raw=t,
    )


class PoolScanner:
    """Unisce i feed Jupiter Tokens v2 e restituisce candidati già arricchiti."""

    # I tre feed sono complementari: `recent` prende i lanci appena avvenuti,
    # `toptrending` chi si sta muovendo ADESSO, `toptraded` dove sta il volume.
    # Ognuno ha la sua cadenza, proporzionata al ricambio misurato: interrogare
    # tutti e tre ad ogni ciclo bruciava budget di rate limit sui due feed che
    # cambiano poco, sottraendolo a `recent` che si rinnova per intero in 75s.
    FEED = (
        ("recent", "/recent", "feed_recent_ogni_sec"),
        ("trending5m", "/toptrending/5m?limit=50", "feed_trending_ogni_sec"),
        ("traded5m", "/toptraded/5m?limit=50", "feed_traded_ogni_sec"),
    )

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        # mint -> timestamp fino al quale NON riproporlo
        self.cooldown: dict[str, float] = {}
        # mint -> motivo, per gli esiti definitivi (comprato / scarto immutabile)
        self.esclusi_definitivi: dict[str, str] = {}
        self.ttl_ricontrollo_sec = 90.0
        self._ultimo_fetch: dict[str, float] = {}
        # Ultima risposta valida per feed: se un feed non va interrogato in
        # questo ciclo (o è in backoff), si riusa il suo contenuto invece di
        # trattarlo come "nessun candidato".
        self._ultima_risposta: dict[str, list] = {}
        self.limitatore = REGISTRO.registra("jupiter", CONFIG.limite_jupiter_al_minuto)
        self._ultimo_errore_log = 0.0

    # ---------------- ESITI ----------------

    def segna_definitivo(self, mint: str, motivo: str):
        """Esclude un mint per sempre: comprato, o scartato per un motivo che
        non può cambiare col tempo (mint authority attiva, dev seriale...)."""
        self.esclusi_definitivi[mint] = motivo

    def segna_ricontrolla(self, mint: str):
        """Esclude un mint solo per il TTL: tornerà valutabile. È il caso di
        "troppo giovane", "volume ancora basso", "momentum non ancora
        partito" — condizioni che cambiano di minuto in minuto."""
        self.cooldown[mint] = time.time() + self.ttl_ricontrollo_sec

    def _scartabile(self, mint: str) -> bool:
        if mint in self.esclusi_definitivi:
            return True
        scadenza = self.cooldown.get(mint)
        if scadenza is None:
            return False
        if time.time() >= scadenza:
            del self.cooldown[mint]
            return False
        return True

    def potatura(self):
        """Tiene i dizionari sotto controllo su una run di giorni."""
        ora = time.time()
        self.cooldown = {m: t for m, t in self.cooldown.items() if t > ora}
        if len(self.esclusi_definitivi) > 20_000:
            self.esclusi_definitivi.clear()
            log.info("Cache esclusioni definitive azzerata (oltre 20k voci)")

    # ---------------- FETCH ----------------

    async def _get_feed(self, nome: str, path: str) -> list[dict]:
        url = f"{CONFIG.api.jupiter_tokens_base}{path}"
        data, errore = await richiesta_json(
            self.session, self.limitatore, "GET", url,
            timeout=aiohttp.ClientTimeout(total=12))

        if errore:
            # Un 429 non è "nessun candidato": va detto, perché la differenza
            # tra "il mercato non offre niente" e "sto sfondando il rate limit"
            # è esattamente ciò che prima restava invisibile.
            if time.time() - self._ultimo_errore_log > 30:
                log.warning("Feed %s non disponibile (%s) — riuso l'ultima risposta valida "
                            "(%d elementi)", nome, errore, len(self._ultima_risposta.get(nome, [])))
                self._ultimo_errore_log = time.time()
            return self._ultima_risposta.get(nome, [])

        lista = data if isinstance(data, list) else []
        self._ultima_risposta[nome] = lista
        return lista

    def _da_interrogare(self, nome: str, attributo: str) -> bool:
        ogni = getattr(CONFIG, attributo, 6.0)
        if time.time() - self._ultimo_fetch.get(nome, 0.0) < ogni:
            return False
        self._ultimo_fetch[nome] = time.time()
        return True

    async def scansiona_nuovi_pool(self) -> list[TokenCandidate]:
        """Interroga i feed che sono scaduti, in PARALLELO, e restituisce i
        candidati unici non in cooldown. Un feed che fallisce non blocca gli
        altri: si riusa la sua ultima risposta valida."""
        da_fare, riusati = [], []
        for nome, path, attributo in self.FEED:
            if self._da_interrogare(nome, attributo):
                da_fare.append((nome, path))
            else:
                riusati.append((nome, self._ultima_risposta.get(nome, [])))

        freschi = await asyncio.gather(*[self._get_feed(n, p) for n, p in da_fare])
        per_feed = list(zip([n for n, _ in da_fare], freschi)) + riusati

        per_mint: dict[str, TokenCandidate] = {}
        totale_grezzi = 0
        for nome, tokens in per_feed:
            for t in tokens:
                totale_grezzi += 1
                mint = t.get("id")
                if not mint or self._scartabile(mint):
                    continue
                if mint in per_mint:
                    # Lo stesso token su più feed è un segnale in sé
                    # (è nuovo E sta scambiando E sta salendo): lo annoto.
                    per_mint[mint].feed = per_mint[mint].feed + (nome,)
                    continue
                cand = _parse_token(t, nome)
                if cand:
                    per_mint[mint] = cand

        candidati = list(per_mint.values())
        self.potatura()
        log.debug("Scansione: %d grezzi → %d candidati unici valutabili (cooldown %d, esclusi %d)",
                  totale_grezzi, len(candidati), len(self.cooldown), len(self.esclusi_definitivi))
        return candidati


async def _test():
    """python scanner.py — controllo rapido che il feed risponda e cosa produce."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s | %(message)s")
    async with aiohttp.ClientSession() as s:
        scanner = PoolScanner(s)
        t0 = time.perf_counter()
        risultati = await scanner.scansiona_nuovi_pool()
        ms = (time.perf_counter() - t0) * 1000
        print(f"\n{len(risultati)} candidati unici in {ms:.0f} ms\n")
        print(f"{'symbol':<14}{'eta_min':>9}{'mcap':>13}{'liq':>11}{'vol5m':>11}"
              f"{'ch5m%':>8}{'organic':>9}{'top%':>7}{'devMints':>9}  feed")
        for c in sorted(risultati, key=lambda x: x.eta_minuti)[:25]:
            top = f"{c.top_holders_pct:.0f}" if c.top_holders_pct is not None else "n/d"
            dm = str(c.dev_mints) if c.dev_mints is not None else "n/d"
            print(f"{c.symbol:<14}{c.eta_minuti:>9.1f}{c.market_cap_usd:>13,.0f}"
                  f"{c.liquidity_usd:>11,.0f}{c.volume_5m_usd:>11,.0f}"
                  f"{c.price_change_5m:>8.1f}{c.organic_score:>9.1f}{top:>7}{dm:>9}"
                  f"  {','.join(c.feed)}")


if __name__ == "__main__":
    asyncio.run(_test())
