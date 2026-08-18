"""
market_conditions.py — Prezzi delle posizioni e Market Calm Score.

DUE RESPONSABILITÀ, DUE FONTI DIVERSE
-------------------------------------
1. PREZZI (percorso caldo, ad alta cadenza). `prezzi()` legge Jupiter Price v3
   in BATCH: una sola richiesta copre tutte le posizioni aperte, misurata a
   ~150 ms. Prima ogni posizione costava una richiesta separata a DexScreener,
   quindi il costo del loop di rischio cresceva col numero di posizioni —
   esattamente al contrario di quello che serve.

2. CALM SCORE (percorso freddo, solo prima di una vendita a tranche). Serve
   la variazione 5m e il flusso buy/sell, che Price v3 non espone: si usa
   DexScreener, ma solo quando una tranche sta per partire.

VARIAZIONE 5m CALCOLATA IN LOCALE
---------------------------------
Il volatility stop ha bisogno della variazione a 5 minuti. Invece di
chiederla a un'API ad ogni ciclo, questo modulo tiene una finestra scorrevole
dei prezzi già letti: il dato è più fresco di qualunque endpoint (deriva dagli
stessi prezzi su cui il bot sta decidendo) e non costa nulla.
"""

import logging
import math
import time
from collections import deque

import aiohttp

from config import CONFIG
from ratelimit import REGISTRO, richiesta_json

log = logging.getLogger("market")

FINESTRA_STORIA_SEC = 360.0


class MarketConditions:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        # mint -> deque[(timestamp, prezzo)]
        self._storia: dict[str, deque] = {}
        self._lim_dex = REGISTRO.registra("dexscreener", CONFIG.limite_dexscreener_al_minuto)
        self._lim_jup = REGISTRO.registra("jupiter", CONFIG.limite_jupiter_al_minuto)

    # ---------------- PREZZI (batch, percorso caldo) ----------------

    async def prezzi(self, mints: list[str]) -> dict[str, dict]:
        """Prezzo, liquidità e variazione 5m per tutti i mint richiesti.

        Fonte primaria DEXSCREENER, non Jupiter, per una ragione di budget:
        Jupiter concede 60 richieste al minuto in tutto, e quel budget serve
        alla discovery e agli swap. Mettendo anche il monitoraggio prezzi sullo
        stesso host si sfonda il tetto e TUTTO smette di funzionare in silenzio
        (misurato: dopo ~90s i feed rispondono 429 e restituiscono zero
        elementi). DexScreener ha un limite indipendente, accetta 30 mint per
        chiamata e restituisce anche la variazione 5m reale di mercato, che
        Price v3 non espone.

        Jupiter Price v3 resta come riserva se DexScreener non risponde.
        """
        if not mints:
            return {}

        out: dict[str, dict] = {}
        # DexScreener accetta fino a 30 indirizzi per richiesta.
        for blocco in [mints[i:i + 30] for i in range(0, len(mints), 30)]:
            url = f"{CONFIG.api.dexscreener_url}/tokens/v1/solana/{','.join(blocco)}"
            data, errore = await richiesta_json(
                self.session, self._lim_dex, "GET", url,
                timeout=aiohttp.ClientTimeout(total=8))
            if errore or not isinstance(data, list):
                log.debug("DexScreener prezzi non disponibile (%s), passo a Price v3", errore)
                out.update(await self._prezzi_jupiter(blocco))
                continue

            # Un token può avere più pool: conta quello con più liquidità.
            migliore: dict[str, dict] = {}
            for p in data:
                mint = (p.get("baseToken") or {}).get("address")
                if not mint:
                    continue
                liq = float((p.get("liquidity") or {}).get("usd") or 0.0)
                if mint not in migliore or liq > migliore[mint]["liquidity"]:
                    migliore[mint] = {
                        "prezzo": float(p.get("priceUsd") or 0.0),
                        "liquidity": liq,
                        "change_5m": float((p.get("priceChange") or {}).get("m5") or 0.0),
                    }
            ora = time.time()
            for mint, v in migliore.items():
                if v["prezzo"] <= 0:
                    continue
                self._registra_e_calcola_5m(mint, v["prezzo"], ora)
                out[mint] = v

            # Mint che DexScreener non conosce (pool troppo fresco): riserva.
            mancanti = [m for m in blocco if m not in out]
            if mancanti:
                out.update(await self._prezzi_jupiter(mancanti))
        return out

    async def _prezzi_jupiter(self, mints: list[str]) -> dict[str, dict]:
        """Riserva su Jupiter Price v3. Consuma budget Jupiter, quindi si usa
        solo per i mint che DexScreener non copre."""
        if not mints:
            return {}
        data, errore = await richiesta_json(
            self.session, self._lim_jup, "GET", CONFIG.api.jupiter_price_url,
            params={"ids": ",".join(mints)}, timeout=aiohttp.ClientTimeout(total=8))
        if errore or not isinstance(data, dict):
            return {}

        ora = time.time()
        out: dict[str, dict] = {}
        for mint, info in data.items():
            if not isinstance(info, dict):
                continue
            prezzo = info.get("usdPrice")
            if not prezzo:
                continue
            out[mint] = {
                "prezzo": float(prezzo),
                "liquidity": float(info.get("liquidity") or 0.0),
                "decimals": int(info.get("decimals") or 6),
                # Price v3 non dà la variazione 5m: si usa la finestra locale.
                "change_5m": self._registra_e_calcola_5m(mint, float(prezzo), ora),
            }
        return out

    def _registra_e_calcola_5m(self, mint: str, prezzo: float, ora: float) -> float | None:
        """Aggiorna la finestra scorrevole e restituisce la variazione
        percentuale sui 5 minuti, o None se la storia non è ancora
        abbastanza lunga per essere significativa."""
        st = self._storia.setdefault(mint, deque())
        st.append((ora, prezzo))
        limite = ora - FINESTRA_STORIA_SEC
        while st and st[0][0] < limite:
            st.popleft()

        obiettivo = ora - 300.0
        riferimento = None
        for ts, px in st:
            if ts <= obiettivo:
                riferimento = px
            else:
                break
        # Serve almeno ~2 minuti di storia perché il numero voglia dire qualcosa
        if riferimento is None:
            if st[0][0] <= ora - 120.0:
                riferimento = st[0][1]
            else:
                return None
        if riferimento <= 0:
            return None
        return (prezzo - riferimento) / riferimento * 100.0

    def dimentica(self, mint: str):
        self._storia.pop(mint, None)

    # ---------------- SNAPSHOT SINGOLO ----------------

    async def snapshot(self, mint: str) -> dict | None:
        """Snapshot arricchito di un singolo token (DexScreener): serve dove
        occorrono flusso e variazione 5m di mercato, non solo il prezzo."""
        url = f"{CONFIG.api.dexscreener_url}/tokens/v1/solana/{mint}"
        pairs, errore = await richiesta_json(
            self.session, self._lim_dex, "GET", url,
            timeout=aiohttp.ClientTimeout(total=8))
        if errore or not pairs:
            return None
        # Il pair con più liquidità, non il primo che capita: un token con più
        # pool va valutato sul mercato che conta davvero.
        return max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))

    async def prezzo_singolo(self, mint: str) -> float | None:
        p = await self.prezzi([mint])
        return p.get(mint, {}).get("prezzo")

    # ---------------- CALM SCORE (percorso freddo) ----------------

    async def calm_score(self, mint: str) -> float:
        """0-100. Su DEX Solana non esiste un order book, quindi lo spread è
        approssimato con proxy on-chain:
          40%  stabilità   → |variazione prezzo 5m| bassa
          35%  liquidità   → liquidità/volume 5m alto (ordini ben assorbiti)
          25%  equilibrio  → buy/sell vicino a 1 (nessun panico direzionale)
        In caso di dati mancanti ritorna 50 (neutro): un 0 prudenziale
        bloccava ogni tranche fino al timeout ogni volta che l'API non
        rispondeva, che è prudenza solo in apparenza."""
        pair = await self.snapshot(mint)
        if not pair:
            return 50.0

        change_5m = abs(float((pair.get("priceChange") or {}).get("m5") or 0))
        vol_5m = float((pair.get("volume") or {}).get("m5") or 0)
        liq = float((pair.get("liquidity") or {}).get("usd") or 0)
        txns = (pair.get("txns") or {}).get("m5") or {}
        buys, sells = int(txns.get("buys") or 0), int(txns.get("sells") or 0)

        stabilita = max(0.0, 100.0 - change_5m * 3.0)
        ratio = liq / max(vol_5m, 1.0)
        liquidita = min(100.0, 100.0 * math.log1p(ratio) / math.log1p(5.0))

        tot = buys + sells
        if tot < 3:
            equilibrio = 40.0
        else:
            equilibrio = 100.0 * (1.0 - abs(buys - sells) / tot)

        score = 0.40 * stabilita + 0.35 * liquidita + 0.25 * equilibrio
        log.debug("Calm %s = %.0f (stab %.0f, liq %.0f, eq %.0f)",
                  mint[:6], score, stabilita, liquidita, equilibrio)
        return round(score, 1)

    async def slippage_stimato_ok(self, mint: str, size_usd: float,
                                  max_pct: float = 8.0) -> bool:
        """Stima dell'impatto: dimensione dell'ordine rispetto alla liquidità.
        Usa Price v3 (una richiesta leggera) invece di DexScreener."""
        p = await self.prezzi([mint])
        liq = p.get(mint, {}).get("liquidity", 0.0)
        if liq <= 0:
            # Senza dato di liquidità non si blocca la vendita: bloccarla
            # significherebbe tenere una posizione che si vuole chiudere.
            return True
        impatto_pct = (size_usd / liq) * 100 * 2  # fattore 2: impatto AMM non lineare
        return impatto_pct <= max_pct
