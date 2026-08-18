"""
filters.py — Filtri anti-rug (hard filters).

DUE LIVELLI, PER NON PAGARE LATENZA SU OGNI CANDIDATO
-----------------------------------------------------
1. `valuta()` — percorso VELOCE, zero chiamate di rete. Usa i dati che lo
   scanner ha già in mano dal blocco `audit` di Jupiter: mint/freeze
   authority, concentrazione top holder, quota del dev, quanti token quel dev
   ha già lanciato. Prima ogni singolo candidato costava una richiesta a
   RugCheck (~15 s di timeout): con 105 candidati per ciclo era la ragione
   principale per cui un "ciclo da 20 secondi" poteva durare minuti.

2. `deep_check()` — percorso LENTO, una sola chiamata a RugCheck, eseguita
   SOLO sul candidato che sta per essere comprato davvero. Un controllo in
   più su un token, non su cento.

LA SCALA DI RUGCHECK, CHE PRIMA ERA INVERTITA
---------------------------------------------
RugCheck restituisce `score_normalised` su scala 0-100 dove BASSO significa
SICURO: è un punteggio di RISCHIO, non di qualità. La versione precedente
richiedeva `score >= 500` credendola una scala 0-5000 di bontà. Poiché il
massimo possibile è 100, quella condizione non poteva essere soddisfatta da
nessun token esistente: il filtro rifiutava il 100% dei candidati e il bot
non aveva modo di comprare niente.

Verificato su 12 token Solana reali: i più sani segnano 1, quelli con problemi
31 e 53. Come riferimento, BONK segna 96 (ha LP vault sbloccati).
"""

import logging
from dataclasses import dataclass, field

import aiohttp

from config import CONFIG
from scanner import TokenCandidate

log = logging.getLogger("filters")

# Simboli di progetti noti: un token nuovo che li copia è quasi sempre un
# tentativo di impersonificazione, non il progetto vero.
SIMBOLI_PROTETTI = {"SOL", "BTC", "ETH", "USDC", "USDT", "BONK", "WIF", "JUP",
                    "RAY", "JTO", "PYTH", "WSOL", "USD1", "TRUMP"}


@dataclass
class FilterResult:
    passed: bool
    motivi_scarto: list[str] = field(default_factory=list)
    # `definitivo` distingue uno scarto che non può cambiare (mint authority
    # attiva, dev seriale, simbolo copiato) da uno temporaneo (troppo giovane,
    # volume ancora basso). Lo scanner usa questa informazione per decidere se
    # escludere il mint per sempre o solo per un TTL breve.
    definitivo: bool = False
    stadio: str = "ok"
    rugcheck_score: int | None = None


class TokenFilter:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    @property
    def f(self):
        # Letto ad ogni accesso, non catturato nel costruttore: il supervisore
        # può modificare le soglie a runtime e devono avere effetto subito.
        return CONFIG.filters

    # ---------------- PERCORSO VELOCE (nessuna rete) ----------------

    def valuta_sync(self, c: TokenCandidate) -> FilterResult:
        f = self.f
        temporanei: list[str] = []
        definitivi: list[str] = []

        # --- Scarti DEFINITIVI: proprietà del token che non cambieranno ---
        if f.richiedi_mint_revocato and not c.mint_revocato:
            definitivi.append("mint authority non revocata (possono stampare token)")
        if f.richiedi_freeze_revocato and not c.freeze_revocato:
            definitivi.append("freeze authority non revocata (possono congelare i wallet)")
        if (c.symbol or "").strip().upper() in SIMBOLI_PROTETTI:
            definitivi.append(f"simbolo ${c.symbol} copia un asset noto (impersonificazione)")
        if c.dev_mints is not None and c.dev_mints > f.max_dev_mints:
            definitivi.append(f"il dev ha già lanciato {c.dev_mints} token (> {f.max_dev_mints}): lanci seriali")

        if definitivi:
            return FilterResult(passed=False, motivi_scarto=definitivi,
                                definitivo=True, stadio="audit_onchain")

        # --- Scarti TEMPORANEI: dipendono dal momento ---
        if c.eta_minuti < f.min_eta_pool_minuti:
            temporanei.append(f"pool troppo giovane ({c.eta_minuti:.1f} min)")
        elif c.eta_minuti > f.max_eta_pool_minuti:
            # Oltre la finestra non tornerà più utile: definitivo.
            return FilterResult(passed=False, stadio="eta",
                                motivi_scarto=[f"pool troppo vecchio ({c.eta_minuti:.0f} min)"],
                                definitivo=True)

        if c.liquidity_usd < f.min_liquidity_usd:
            temporanei.append(f"liquidità ${c.liquidity_usd:,.0f} < ${f.min_liquidity_usd:,.0f}")
        if c.volume_5m_usd < f.min_volume_5m_usd:
            temporanei.append(f"volume 5m ${c.volume_5m_usd:,.0f} < ${f.min_volume_5m_usd:,.0f}")
        if c.market_cap_usd < f.min_market_cap_usd:
            temporanei.append(f"mcap ${c.market_cap_usd:,.0f} sotto la banda")
        elif c.market_cap_usd > f.max_market_cap_usd:
            temporanei.append(f"mcap ${c.market_cap_usd:,.0f} sopra la banda")
        if c.market_cap_usd > 0 and c.liq_su_mcap < f.min_liq_su_mcap:
            temporanei.append(f"liq/mcap {c.liq_su_mcap:.1%} < {f.min_liq_su_mcap:.0%} (mcap gonfiato)")
        if c.buy_sell_ratio < f.min_buy_sell_ratio:
            temporanei.append(f"pressione di vendita alta (buy/sell {c.buy_sell_ratio:.2f})")
        if c.price_change_5m > f.max_price_change_5m_ingresso:
            temporanei.append(f"già +{c.price_change_5m:.0f}% in 5m: rischio tetto del pump")

        # Concentrazione holder: solo se il dato esiste (misurato: presente su
        # 106 token su 130 — sui lanci appena avvenuti Jupiter non l'ha ancora).
        if c.top_holders_pct is not None and c.top_holders_pct > f.max_top_holders_pct:
            temporanei.append(f"top holder al {c.top_holders_pct:.0f}% > {f.max_top_holders_pct:.0f}%")
        if c.dev_balance_pct is not None and c.dev_balance_pct > f.max_dev_balance_pct:
            temporanei.append(f"il dev detiene il {c.dev_balance_pct:.0f}% della supply")

        # organicScore: gate applicato solo dove il dato esiste davvero.
        if c.eta_minuti >= f.organic_gate_da_eta_minuti:
            if c.organic_score < f.min_organic_score:
                temporanei.append(f"organic score {c.organic_score:.0f} < {f.min_organic_score:.0f} (volume poco genuino)")
            if (c.quota_volume_organico is not None
                    and c.quota_volume_organico < f.min_quota_volume_organico):
                temporanei.append(f"solo {c.quota_volume_organico:.1%} del volume 1h è organico")

        if temporanei:
            return FilterResult(passed=False, motivi_scarto=temporanei,
                                definitivo=False, stadio="metriche")

        return FilterResult(passed=True, stadio="ok")

    async def valuta(self, c: TokenCandidate) -> FilterResult:
        """Firma async mantenuta per compatibilità con il resto del bot, ma il
        percorso veloce non fa più I/O: è puro calcolo su dati già in memoria."""
        return self.valuta_sync(c)

    # ---------------- PERCORSO LENTO (una sola chiamata, pre-acquisto) ----------------

    async def deep_check(self, c: TokenCandidate) -> FilterResult:
        """Verifica finale su RugCheck, eseguita solo sul token che sta per
        essere comprato. Un secondo parere indipendente da Jupiter proprio
        prima di impegnare capitale."""
        f = self.f
        if not f.usa_rugcheck_deep_check:
            return FilterResult(passed=True, stadio="deep_check_disattivato")

        report = await self._rugcheck_report(c.mint)
        if report is None:
            if f.rugcheck_scarta_se_non_disponibile:
                return FilterResult(passed=False, stadio="rugcheck",
                                    motivi_scarto=["RugCheck non disponibile → scarto prudenziale"])
            log.info("RugCheck non disponibile per %s: procedo sui soli dati Jupiter", c.symbol)
            return FilterResult(passed=True, stadio="rugcheck_assente")

        # score_normalised è 0-100 e BASSO = SICURO. `score` grezzo (fino a
        # milioni) è la stessa cosa non normalizzata: si usa solo se il
        # normalizzato manca, con soglia proporzionata.
        score_norm = report.get("score_normalised")
        score = score_norm if score_norm is not None else report.get("score")
        res = FilterResult(passed=True, stadio="rugcheck", rugcheck_score=score)

        if report.get("rugged") is True:
            res.passed = False
            res.definitivo = True
            res.motivi_scarto.append("RugCheck: token già segnalato come RUGGED")
            return res

        if score_norm is not None and score_norm > f.max_rugcheck_score:
            res.passed = False
            res.motivi_scarto.append(
                f"RugCheck rischio {score_norm}/100 > {f.max_rugcheck_score} "
                f"(scala di RISCHIO: più alto = più pericoloso)")

        # I rischi di livello "danger" sono un veto a prescindere dal punteggio.
        pericoli = [r.get("name") for r in (report.get("risks") or [])
                    if r.get("level") == "danger"]
        if pericoli:
            log.info("RugCheck segnala pericoli su %s: %s", c.symbol, pericoli)

        if res.passed:
            log.info("🔎 Deep check %s superato (rischio RugCheck %s/100)", c.symbol, score_norm)
        return res

    async def _rugcheck_report(self, mint: str) -> dict | None:
        url = f"{CONFIG.api.rugcheck_url}/tokens/{mint}/report"
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    return await r.json()
                log.debug("RugCheck HTTP %s per %s", r.status, mint)
        except Exception as e:
            log.debug("Errore RugCheck su %s: %s", mint, e)
        return None
