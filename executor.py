"""
executor.py — Esecuzione degli swap via Jupiter Aggregator (API v6).

Flusso di uno swap:
  1. GET /quote  → miglior rotta SOL→token (o token→SOL in vendita)
  2. POST /swap  → Jupiter costruisce la transazione serializzata
  3. Firma locale con la chiave del wallet (solders)
  4. Invio via RPC Helius (o Jito) + CONFERMA on-chain + lettura dell'esito reale

In modalità "paper" nessuna transazione viene inviata: il trade è simulato
e registrato su file per validare la strategia senza rischiare capitale.

Garanzie anti-perdita-di-tracciamento (in modalità live):
  - Prima di comprare, verifica il saldo SOL REALE del wallet (non il file di
    stato interno) e rifiuta il trade se insufficiente: mai un tentativo a vuoto
    che brucia solo fee.
  - Dopo ogni invio, ATTENDE la conferma on-chain (getSignatureStatuses) prima
    di dichiarare successo: niente più "trade fantasma" spediti ma mai atterrati.
  - L'importo realmente ricevuto/speso viene letto dai balance pre/post della
    transazione confermata (getTransaction), MAI dalla quote preventivata:
    lo slippage reale non genera più disallineamento tra ciò che il bot pensa
    di avere e ciò che ha davvero in wallet.
"""

import asyncio
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

MARGINE_FEE_SOL = 0.01          # riserva minima per priority fee + tip Jito + gas
TIMEOUT_CONFERMA_SEC = 45.0     # quanto aspettare che una tx sia confirmed/finalized
POLL_CONFERMA_SEC = 2.0


@dataclass
class SwapResult:
    ok: bool
    firma_tx: str | None = None
    input_amount: float = 0.0
    output_amount: float = 0.0
    prezzo_effettivo: float = 0.0
    errore: str | None = None
    simulato: bool = False
    # Unità RAW di token effettivamente vendute (solo per operazioni di vendita).
    # Usato dal chiamante per aggiornare la posizione SOLO in base a ciò che è
    # davvero uscito dal wallet, mai in base a ciò che era stato richiesto.
    unita_vendute_raw: int = 0


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

    # ---------------- SALDO REALE (backstop indipendente dalla contabilità interna) ----------------

    async def saldo_sol(self) -> float:
        """Saldo SOL reale del wallet letto on-chain. Mai fidarsi solo di stato_bot.json."""
        if not self.keypair:
            return 0.0
        body = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [str(self.keypair.pubkey())]}
        try:
            async with self.session.post(
                CONFIG.api.rpc_url, json=body, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                data = await r.json()
            lamports = (data.get("result") or {}).get("value", 0)
            return lamports / LAMPORTS_PER_SOL
        except Exception as e:
            log.error("Errore lettura saldo wallet: %s", e)
            return 0.0

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
        if not self.paper_mode:
            saldo = await self.saldo_sol()
            necessario = sol_amount + MARGINE_FEE_SOL
            if saldo < necessario:
                return SwapResult(
                    ok=False,
                    errore=f"saldo SOL reale insufficiente: {saldo:.4f} disponibili, "
                           f"servono {necessario:.4f} ({sol_amount:.4f} + {MARGINE_FEE_SOL} di margine fee)",
                )
        lamports = int(sol_amount * LAMPORTS_PER_SOL)
        q = await self.quote(SOL_MINT, token_mint, lamports)
        if not q:
            return SwapResult(ok=False, errore="quote non disponibile")
        return await self._esegui(q, sol_amount, mint_atteso=token_mint, is_buy=True)

    async def vendi(self, token_mint: str, token_amount_raw: int) -> SwapResult:
        """Vendi `token_amount_raw` (unità minime del token) in SOL."""
        if token_amount_raw <= 0:
            return SwapResult(ok=False, errore="quantità da vendere nulla")
        q = await self.quote(token_mint, SOL_MINT, token_amount_raw)
        if not q:
            return SwapResult(ok=False, errore="quote non disponibile")
        res = await self._esegui(q, token_amount_raw, mint_atteso=token_mint, is_buy=False)
        if res.ok:
            # Uno swap Solana è atomico: se la tx è confermata, TUTTO l'input
            # richiesto è stato consumato — nessuna esecuzione parziale possibile
            # a livello di singola transazione.
            res.unita_vendute_raw = token_amount_raw
        return res

    async def vendi_tranches(self, token_mint: str, token_amount_raw: int,
                             market, size_usd_totale: float) -> SwapResult:
        """
        Vendita parzializzata: divide l'ordine in N tranche e vende ognuna
        solo quando il market calm score supera la soglia (o allo scadere
        del timeout). Riduce slippage e impatto sul prezzo.

        IMPORTANTE: se una tranche fallisce dopo che altre sono andate a buon
        fine, il risultato riporta `unita_vendute_raw` = SOLO ciò che è stato
        davvero venduto (mai l'intero importo richiesto). Il chiamante deve
        usare questo valore, non la frazione originariamente richiesta, per
        aggiornare la posizione — altrimenti si perde traccia di token reali
        rimasti in wallet ma non più monitorati.
        """
        r = CONFIG.risk
        n = max(1, r.tranches_per_vendita)
        tranche_qty = token_amount_raw // n
        venduto_raw, ricavato = 0, 0.0
        ultima_firma = None
        ultimo_errore = None

        for i in range(n):
            qty = tranche_qty if i < n - 1 else token_amount_raw - venduto_raw  # resto nell'ultima
            deadline = time.time() + r.calm_timeout_min * 60

            while time.time() < deadline:
                calm = await market.calm_score(token_mint)
                slippage_ok = await market.slippage_stimato_ok(
                    token_mint, size_usd_totale / n, r.max_slippage_tranche_pct)
                if calm >= r.calm_soglia_vendita and slippage_ok:
                    break
                log.info("⏸️  Tranche %d/%d in attesa (calm=%.0f, soglia=%.0f)", i + 1, n, calm, r.calm_soglia_vendita)
                await asyncio.sleep(r.calm_check_sec)
            else:
                log.warning("⏰ Timeout calm su tranche %d/%d → vendo comunque", i + 1, n)

            res = await self.vendi(token_mint, qty)
            if not res.ok:
                ultimo_errore = res.errore
                log.error("Tranche %d/%d fallita: %s", i + 1, n, res.errore)
                break  # niente retry automatico: meglio fermarsi e riportare il parziale con precisione
            venduto_raw += res.unita_vendute_raw
            ricavato += res.output_amount
            ultima_firma = res.firma_tx
            log.info("✅ Tranche %d/%d eseguita (calm ok)", i + 1, n)

        return SwapResult(
            ok=venduto_raw > 0,
            firma_tx=ultima_firma,
            input_amount=venduto_raw,
            output_amount=ricavato,
            simulato=self.paper_mode,
            unita_vendute_raw=venduto_raw,
            errore=None if venduto_raw >= token_amount_raw else (ultimo_errore or "vendita parziale"),
        )

    # ---------------- CORE ----------------

    async def _esegui(self, quote: dict, input_amount: float,
                      mint_atteso: str | None = None, is_buy: bool = True) -> SwapResult:
        out_amount_stimato = float(quote.get("outAmount", 0))

        if self.paper_mode:
            log.info("[PAPER] Swap simulato: in=%s out=%s", input_amount, out_amount_stimato)
            self._log_paper_trade(quote)
            return SwapResult(
                ok=True, firma_tx="PAPER-" + str(int(time.time())),
                input_amount=input_amount, output_amount=out_amount_stimato, simulato=True,
            )

        # --- LIVE: costruisci, firma, invia, ATTENDI CONFERMA e leggi l'esito reale ---
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
            # La firma è intrinseca alla transazione firmata: è la stessa a
            # prescindere dal canale di invio (Jito bundle o RPC diretto), quindi
            # è l'UNICO identificatore affidabile per interrogare poi la conferma.
            firma = str(tx_firmata.signatures[0])

            inviata = await self._invia_tx(bytes(tx_firmata))
            if not inviata:
                return SwapResult(ok=False, errore="invio transazione fallito")

            esito = await self._conferma_e_leggi_esito(firma, str(self.keypair.pubkey()), mint_atteso, is_buy)
            if esito is None:
                return SwapResult(ok=False, errore="transazione non confermata entro il timeout (o fallita on-chain)", firma_tx=firma)

            out_amount_reale = esito["amount"]
            if out_amount_reale <= 0:
                return SwapResult(ok=False, errore="importo ricevuto nullo/negativo dopo conferma", firma_tx=firma)

            log.info("✅ Swap confermato: %s (atteso≈%s, reale=%s)", firma, out_amount_stimato, out_amount_reale)
            return SwapResult(ok=True, firma_tx=firma,
                              input_amount=input_amount, output_amount=float(out_amount_reale))
        except Exception as e:
            log.error("Errore esecuzione swap: %s", e)
            return SwapResult(ok=False, errore=str(e))

    # ---------------- CONFERMA E LETTURA ESITO REALE ----------------

    async def _conferma_e_leggi_esito(self, firma: str, wallet_pubkey: str,
                                      mint_atteso: str | None, is_buy: bool) -> dict | None:
        """Attende che la tx sia confirmed/finalized, poi legge l'importo REALE
        dai balance pre/post della transazione (mai dalla quote preventivata)."""
        deadline = time.time() + TIMEOUT_CONFERMA_SEC
        while time.time() < deadline:
            try:
                body = {
                    "jsonrpc": "2.0", "id": 1, "method": "getSignatureStatuses",
                    "params": [[firma], {"searchTransactionHistory": True}],
                }
                async with self.session.post(
                    CONFIG.api.rpc_url, json=body, timeout=aiohttp.ClientTimeout(total=10)
                ) as r:
                    data = await r.json()
                stato = ((data.get("result") or {}).get("value") or [None])[0]
                if stato:
                    if stato.get("err"):
                        log.error("❌ Transazione fallita on-chain (%s): %s", firma, stato["err"])
                        return None
                    if stato.get("confirmationStatus") in ("confirmed", "finalized"):
                        return await self._leggi_esito_tx(firma, wallet_pubkey, mint_atteso, is_buy)
            except Exception as e:
                log.warning("Errore poll conferma %s: %s", firma, e)
            await asyncio.sleep(POLL_CONFERMA_SEC)
        log.error("⏰ Timeout conferma transazione %s dopo %.0fs", firma, TIMEOUT_CONFERMA_SEC)
        return None

    async def _leggi_esito_tx(self, firma: str, wallet_pubkey: str,
                              mint_atteso: str | None, is_buy: bool) -> dict | None:
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
            "params": [firma, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        }
        try:
            async with self.session.post(
                CONFIG.api.rpc_url, json=body, timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                data = await r.json()
        except Exception as e:
            log.error("Errore lettura esito tx %s: %s", firma, e)
            return None

        tx = data.get("result")
        if not tx or not tx.get("meta"):
            return None
        meta = tx["meta"]

        if is_buy:
            pre = {b["accountIndex"]: b for b in (meta.get("preTokenBalances") or [])
                   if b.get("mint") == mint_atteso and b.get("owner") == wallet_pubkey}
            post = {b["accountIndex"]: b for b in (meta.get("postTokenBalances") or [])
                    if b.get("mint") == mint_atteso and b.get("owner") == wallet_pubkey}
            pre_amt = sum(int(b["uiTokenAmount"]["amount"]) for b in pre.values())
            post_amt = sum(int(b["uiTokenAmount"]["amount"]) for b in post.values())
            return {"amount": post_amt - pre_amt}

        # Sell: ci interessa il delta di SOL (lamports) del wallet
        try:
            keys = tx["transaction"]["message"]["accountKeys"]
            idx = next(
                (i for i, k in enumerate(keys)
                 if (k.get("pubkey") if isinstance(k, dict) else k) == wallet_pubkey),
                None,
            )
            if idx is None:
                return None
            delta = meta["postBalances"][idx] - meta["preBalances"][idx]
            return {"amount": delta}
        except (KeyError, IndexError, TypeError) as e:
            log.error("Errore parsing esito vendita %s: %s", firma, e)
            return None

    # ---------------- INVIO (Jito con fallback RPC) ----------------
    # Photon non offre un'API per bot (verificato: è un terminale manuale).
    # La vera leva di velocità che i trader "veloci" usano è instradare la tx
    # tramite Jito, che la include nel prossimo blocco pagando un "tip" ai
    # validator invece di aspettare la coda pubblica dell'RPC.

    async def _invia_via_jito(self, tx_bytes: bytes) -> bool:
        """Invia la transazione firmata a un Jito Block Engine invece che al solo RPC pubblico.
        NB: il "result" di sendBundle è l'ID del bundle, NON la firma della
        transazione — non va mai usato per interrogare la conferma."""
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
                    return False
                data = await r.json()
                if "error" in data:
                    log.warning("Jito error: %s, fallback su RPC normale", data["error"])
                    return False
                return True
        except Exception as e:
            log.warning("Jito non disponibile (%s), fallback su RPC normale", e)
            return False

    async def _invia_tx(self, tx_bytes: bytes) -> bool:
        # Prova prima Jito (ingresso prioritario, salta la coda pubblica).
        # Se non disponibile o fallisce: fallback trasparente sull'RPC Helius normale.
        if CONFIG.risk.usa_jito:
            if await self._invia_via_jito(tx_bytes):
                log.info("⚡ Inviato via Jito bundle (ingresso prioritario)")
                return True

        body = {
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [
                base64.b64encode(tx_bytes).decode(),
                {"encoding": "base64", "skipPreflight": True, "maxRetries": 3},
            ],
        }
        try:
            async with self.session.post(CONFIG.api.rpc_url, json=body) as r:
                data = await r.json()
                if "error" in data:
                    log.error("RPC error: %s", data["error"])
                    return False
                return True
        except Exception as e:
            log.error("Errore invio RPC: %s", e)
            return False

    @staticmethod
    def _log_paper_trade(quote: dict):
        with open("paper_trades.jsonl", "a") as f:
            f.write(json.dumps({"ts": time.time(), "quote": quote}) + "\n")
