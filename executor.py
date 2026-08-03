"""
executor.py — Esecuzione degli swap via Jupiter Aggregator (API v6).

Flusso di uno swap:
  1. GET /quote  → miglior rotta SOL→token (o token→SOL in vendita)
  2. POST /swap  → Jupiter costruisce la transazione serializzata
  3. Firma locale con la chiave del wallet (solders)
  4. Invio via RPC Helius + conferma

In modalità "paper" nessuna transazione viene inviata: il trade è simulato
e registrato su file per validare la strategia senza rischiare capitale.
"""

import base64
import json
import logging
import time
from dataclasses import dataclass

import aiohttp
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from config import CONFIG

log = logging.getLogger("executor")

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000


@dataclass
class SwapResult:
    ok: bool
    firma_tx: str | None = None
    input_amount: float = 0.0
    output_amount: float = 0.0
    prezzo_effettivo: float = 0.0
    errore: str | None = None
    simulato: bool = False


class JupiterExecutor:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.paper_mode = CONFIG.mode != "live"
        self.keypair: Keypair | None = None
        if not self.paper_mode:
            if not CONFIG.api.wallet_private_key:
                raise RuntimeError("Modalità live ma WALLET_PRIVATE_KEY mancante in .env")
            self.keypair = Keypair.from_base58_string(CONFIG.api.wallet_private_key)
            log.info("Wallet caricato: %s", self.keypair.pubkey())

    # ---------------- QUOTE ----------------

    async def quote(self, input_mint: str, output_mint: str, amount_lamports: int) -> dict | None:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_lamports),
            "slippageBps": str(CONFIG.risk.slippage_bps),
        }
        try:
            async with self.session.get(
                CONFIG.api.jupiter_quote_url, params=params,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status == 200:
                    return await r.json()
                log.error("Jupiter quote HTTP %s: %s", r.status, await r.text())
        except Exception as e:
            log.error("Errore quote: %s", e)
        return None

    # ---------------- BUY / SELL ----------------

    async def compra(self, token_mint: str, sol_amount: float) -> SwapResult:
        """Compra `token_mint` spendendo `sol_amount` SOL."""
        lamports = int(sol_amount * LAMPORTS_PER_SOL)
        q = await self.quote(SOL_MINT, token_mint, lamports)
        if not q:
            return SwapResult(ok=False, errore="quote non disponibile")
        return await self._esegui(q, sol_amount)

    async def vendi(self, token_mint: str, token_amount_raw: int) -> SwapResult:
        """Vendi `token_amount_raw` (unità minime del token) in SOL."""
        q = await self.quote(token_mint, SOL_MINT, token_amount_raw)
        if not q:
            return SwapResult(ok=False, errore="quote non disponibile")
        return await self._esegui(q, token_amount_raw)

    async def vendi_tranches(self, token_mint: str, token_amount_raw: int,
                             market, size_usd_totale: float) -> SwapResult:
        """
        Vendita parzializzata: divide l'ordine in N tranche e vende ognuna
        solo quando il market calm score supera la soglia (o allo scadere
        del timeout). Riduce slippage e impatto sul prezzo.

        Fallback: qualsiasi problema con il calm score → vendita immediata.
        Nelle uscite l'errore peggiore è NON vendere, non vendere male.
        """
        import asyncio
        r = CONFIG.risk
        n = max(1, r.tranches_per_vendita)
        tranche_qty = token_amount_raw // n
        tranche_usd = size_usd_totale / n
        venduto_raw, ricavato = 0, 0.0
        ultima_firma = None

        for i in range(n):
            qty = tranche_qty if i < n - 1 else token_amount_raw - venduto_raw  # resto nell'ultima
            deadline = time.time() + r.calm_timeout_min * 60

            while time.time() < deadline:
                calm = await market.calm_score(token_mint)
                slippage_ok = await market.slippage_stimato_ok(
                    token_mint, tranche_usd, r.max_slippage_tranche_pct)
                if calm >= r.calm_soglia_vendita and slippage_ok:
                    break
                log.info("⏸️  Tranche %d/%d in attesa (calm=%.0f, soglia=%.0f)", i + 1, n, calm, r.calm_soglia_vendita)
                await asyncio.sleep(r.calm_check_sec)
            else:
                log.warning("⏰ Timeout calm su tranche %d/%d → vendo comunque", i + 1, n)

            res = await self.vendi(token_mint, qty)
            if not res.ok:
                log.error("Tranche %d/%d fallita: %s", i + 1, n, res.errore)
                if venduto_raw == 0:
                    return res  # niente venduto: riporta l'errore al chiamante
                break           # vendita parziale: restituisci quanto fatto
            venduto_raw += qty
            ricavato += res.output_amount
            ultima_firma = res.firma_tx
            log.info("✅ Tranche %d/%d eseguita (calm ok)", i + 1, n)

        return SwapResult(ok=True, firma_tx=ultima_firma,
                          input_amount=venduto_raw, output_amount=ricavato,
                          simulato=self.paper_mode)

    # ---------------- CORE ----------------

    async def _esegui(self, quote: dict, input_amount: float) -> SwapResult:
        out_amount = float(quote.get("outAmount", 0))

        if self.paper_mode:
            log.info("[PAPER] Swap simulato: in=%s out=%s", input_amount, out_amount)
            self._log_paper_trade(quote)
            return SwapResult(
                ok=True, firma_tx="PAPER-" + str(int(time.time())),
                input_amount=input_amount, output_amount=out_amount, simulato=True,
            )

        # --- LIVE: costruisci, firma e invia la transazione ---
        # Se usa_jito è attivo, chiediamo a Jupiter di includere l'istruzione
        # di tip Jito direttamente nella tx (supporto nativo dell'API v6):
        # più affidabile che costruirla a mano con un tip-account hardcoded.
        prio_fee = (
            {"jitoTipLamports": CONFIG.risk.jito_tip_lamports}
            if CONFIG.risk.usa_jito
            else CONFIG.risk.priority_fee_microlamports
        )
        body = {
            "quoteResponse": quote,
            "userPublicKey": str(self.keypair.pubkey()),
            "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": prio_fee,
            "dynamicComputeUnitLimit": True,
        }
        try:
            async with self.session.post(
                CONFIG.api.jupiter_swap_url, json=body,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                if r.status != 200:
                    return SwapResult(ok=False, errore=f"swap build HTTP {r.status}")
                swap_data = await r.json()

            raw_tx = base64.b64decode(swap_data["swapTransaction"])
            tx = VersionedTransaction.from_bytes(raw_tx)
            tx_firmata = VersionedTransaction(tx.message, [self.keypair])

            firma = await self._invia_tx(bytes(tx_firmata))
            if not firma:
                return SwapResult(ok=False, errore="invio transazione fallito")

            log.info("✅ Swap eseguito: %s", firma)
            return SwapResult(ok=True, firma_tx=firma,
                              input_amount=input_amount, output_amount=out_amount)
        except Exception as e:
            log.error("Errore esecuzione swap: %s", e)
            return SwapResult(ok=False, errore=str(e))

    # ---------------- JITO BUNDLE (velocità reale, alternativa a Photon) ----------------
    # Photon non offre un'API per bot (verificato: è un terminale manuale).
    # La vera leva di velocità che i trader "veloci" usano è instradare la tx
    # tramite Jito, che la include nel prossimo blocco pagando un "tip" ai
    # validator invece di aspettare la coda pubblica dell'RPC.

    async def _invia_via_jito(self, tx_bytes: bytes) -> str | None:
        """Invia la transazione firmata a un Jito Block Engine invece che al solo RPC pubblico."""
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "sendBundle",
            "params": [[base64.b64encode(tx_bytes).decode()]],
        }
        try:
            async with self.session.post(
                CONFIG.api.jito_block_engine_url, json=body,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    log.warning("Jito HTTP %s, fallback su RPC normale", r.status)
                    return None
                data = await r.json()
                if "error" in data:
                    log.warning("Jito error: %s, fallback su RPC normale", data["error"])
                    return None
                return data.get("result")
        except Exception as e:
            log.warning("Jito non disponibile (%s), fallback su RPC normale", e)
            return None

    async def _invia_tx(self, tx_bytes: bytes) -> str | None:
        # Prova prima Jito (ingresso prioritario, salta la coda pubblica).
        # Se non disponibile o fallisce: fallback trasparente sull'RPC Helius normale.
        if CONFIG.risk.usa_jito:
            firma = await self._invia_via_jito(tx_bytes)
            if firma:
                log.info("⚡ Inviato via Jito bundle (ingresso prioritario)")
                return firma

        body = {
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [
                base64.b64encode(tx_bytes).decode(),
                {"encoding": "base64", "skipPreflight": True, "maxRetries": 3},
            ],
        }
        async with self.session.post(CONFIG.api.rpc_url, json=body) as r:
            data = await r.json()
            if "error" in data:
                log.error("RPC error: %s", data["error"])
                return None
            return data.get("result")

    @staticmethod
    def _log_paper_trade(quote: dict):
        with open("paper_trades.jsonl", "a") as f:
            f.write(json.dumps({"ts": time.time(), "quote": quote}) + "\n")
