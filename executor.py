"""
executor.py — Esecuzione degli swap via Jupiter Swap API v1.

Flusso di uno swap:
  1. GET /swap/v1/quote  → miglior rotta SOL→token (o token→SOL in vendita)
  2. POST /swap/v1/swap  → Jupiter costruisce la transazione serializzata
  3. Firma locale con la chiave del wallet (solders)
  4. Invio via Jito bundle o RPC Helius + CONFERMA on-chain + lettura
     dell'importo REALE dai balance pre/post della transazione

In modalità "paper" nessuna transazione viene inviata.

CORREZIONI RISPETTO ALLA VERSIONE PRECEDENTE
--------------------------------------------
- ENDPOINT: puntava a `quote-api.jup.ag/v6`, host spento da Jupiter che oggi
  non risolve nemmeno in DNS. Ogni quote ritornava None, quindi ogni acquisto
  e ogni vendita di emergenza fallivano. Ora `lite-api.jup.ag/swap/v1`,
  verificato funzionante con lo stesso corpo di richiesta.

- SLIPPAGE: 3% fisso faceva fallire in preflight gran parte degli swap su
  memecoin fresche. Ora `dynamicSlippage`, con Jupiter che calcola il valore
  per rotta e restituisce un `dynamicSlippageReport`.

- JITO: il bundle veniva inviato in base64 SENZA dichiarare `encoding`. La
  documentazione Jito è esplicita: il default è base58. Il block engine
  rifiutava ogni bundle, il codice ricadeva sull'RPC normale ad ogni trade e
  il vantaggio di velocità per cui esiste tutto quel percorso non c'è mai
  stato. Ora l'encoding è dichiarato.

- CONFERMA TARDIVA: se la conferma non arrivava entro il timeout, `compra()`
  ritornava un fallimento e il bot non registrava la posizione — ma la
  transazione poteva atterrare dopo, lasciando token reali nel wallet che
  nessuno monitorava (nessuno stop loss, nessun ladder). Ora prima di
  dichiarare fallito un acquisto si rilegge il saldo token reale del mint.
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
from ratelimit import REGISTRO, richiesta_json

log = logging.getLogger("executor")

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000

MARGINE_FEE_SOL = 0.012   # riserva per priority fee + tip Jito + rent ATA


@dataclass
class SwapResult:
    ok: bool
    firma_tx: str | None = None
    input_amount: float = 0.0
    output_amount: float = 0.0
    prezzo_effettivo: float = 0.0
    errore: str | None = None
    simulato: bool = False
    # Unità RAW di token effettivamente vendute (solo per le vendite). Il
    # chiamante deve aggiornare la posizione SOLO in base a questo valore, mai
    # in base a quanto era stato richiesto.
    unita_vendute_raw: int = 0
    # True se il risultato è stato ricostruito dal saldo on-chain dopo un
    # timeout di conferma, invece che letto dalla transazione confermata.
    recuperato_da_saldo: bool = False


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
        # Cache del saldo SOL: il controllo di sicurezza gira ad alta cadenza e
        # non deve trasformarsi in una chiamata RPC per ciclo.
        self._saldo_cache: float = 0.0
        self._saldo_cache_ts: float = 0.0
        # Quote e swap condividono il budget Jupiter con la discovery: al
        # momento di un trade hanno la priorità, ma devono comunque passare
        # dal limitatore per non far scattare un 429 che spegnerebbe i feed.
        self._lim = REGISTRO.registra("jupiter", CONFIG.limite_jupiter_al_minuto)

    # ---------------- SALDO REALE ----------------

    async def saldo_sol(self, max_eta_sec: float | None = None) -> float:
        """Saldo SOL reale letto on-chain. Mai fidarsi solo di stato_bot.json."""
        if not self.keypair:
            return 0.0
        ttl = CONFIG.saldo_refresh_sec if max_eta_sec is None else max_eta_sec
        if ttl > 0 and (time.time() - self._saldo_cache_ts) < ttl:
            return self._saldo_cache
        body = {"jsonrpc": "2.0", "id": 1, "method": "getBalance",
                "params": [str(self.keypair.pubkey())]}
        try:
            async with self.session.post(
                CONFIG.api.rpc_url, json=body, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                data = await r.json()
            lamports = (data.get("result") or {}).get("value", 0)
            self._saldo_cache = lamports / LAMPORTS_PER_SOL
            self._saldo_cache_ts = time.time()
            return self._saldo_cache
        except Exception as e:
            log.error("Errore lettura saldo wallet: %s", e)
            return self._saldo_cache

    async def saldo_token_raw(self, mint: str) -> int:
        """Saldo reale del token nel wallet, in unità minime. Serve sia a
        recuperare le posizioni dopo una conferma tardiva, sia a vendere
        esattamente ciò che c'è invece di ciò che la contabilità crede."""
        if not self.keypair:
            return 0
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
            "params": [str(self.keypair.pubkey()), {"mint": mint},
                       {"encoding": "jsonParsed"}],
        }
        try:
            async with self.session.post(
                CONFIG.api.rpc_url, json=body, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                data = await r.json()
            totale = 0
            for acct in ((data.get("result") or {}).get("value") or []):
                info = acct["account"]["data"]["parsed"]["info"]
                totale += int(info["tokenAmount"]["amount"])
            return totale
        except Exception as e:
            log.error("Errore lettura saldo token %s: %s", mint, e)
            return 0

    # ---------------- QUOTE ----------------

    async def quote(self, input_mint: str, output_mint: str, amount: int) -> dict | None:
        r_cfg = CONFIG.risk
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "restrictIntermediateTokens": "true",
        }
        if r_cfg.usa_dynamic_slippage:
            params["dynamicSlippage"] = "true"
            params["maxAutoSlippageBps"] = str(r_cfg.max_slippage_bps_dinamico)
        else:
            params["slippageBps"] = str(r_cfg.slippage_bps)
        data, errore = await richiesta_json(
            self.session, self._lim, "GET", CONFIG.api.jupiter_quote_url,
            params=params, timeout=aiohttp.ClientTimeout(total=8))
        if errore:
            log.warning("Quote %s→%s non disponibile: %s",
                        input_mint[:6], output_mint[:6], errore)
            return None
        return data

    # ---------------- BUY / SELL ----------------

    async def compra(self, token_mint: str, sol_amount: float) -> SwapResult:
        """Compra `token_mint` spendendo `sol_amount` SOL."""
        if not self.paper_mode:
            # Saldo forzato fresco: qui si sta per impegnare capitale reale.
            saldo = await self.saldo_sol(max_eta_sec=0)
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
        """Vendi `token_amount_raw` (unità minime) in SOL."""
        if token_amount_raw <= 0:
            return SwapResult(ok=False, errore="quantità da vendere nulla")

        # Non provare a vendere più di quanto c'è davvero: dopo una vendita
        # parziale o un recupero, la contabilità interna può divergere dal
        # wallet, e uno swap con importo maggiore del saldo fallisce del tutto
        # invece di vendere il residuo.
        if not self.paper_mode:
            reale = await self.saldo_token_raw(token_mint)
            if reale <= 0:
                return SwapResult(ok=False, errore="nessun token di questo mint nel wallet")
            if reale < token_amount_raw:
                log.warning("Vendita %s: richiesti %d, nel wallet %d → vendo il reale",
                            token_mint[:6], token_amount_raw, reale)
                token_amount_raw = reale

        q = await self.quote(token_mint, SOL_MINT, token_amount_raw)
        if not q:
            return SwapResult(ok=False, errore="quote non disponibile")
        res = await self._esegui(q, token_amount_raw, mint_atteso=token_mint, is_buy=False)
        if res.ok:
            # Uno swap Solana è atomico: se la tx è confermata, tutto l'input
            # richiesto è stato consumato. Nessuna esecuzione parziale
            # possibile a livello di singola transazione.
            res.unita_vendute_raw = token_amount_raw
        return res

    async def vendi_tranches(self, token_mint: str, token_amount_raw: int,
                             market, size_usd_totale: float,
                             urgente: bool = False) -> SwapResult:
        """
        Vendita parzializzata calm-aware.

        La versione precedente attendeva il market calm con un timeout di
        `calm_timeout_min` MINUTI per tranche (15) su 3 tranche: fino a 45
        minuti dentro cui il loop principale del bot restava bloccato, senza
        scansioni, senza controllo del saldo e con le altre posizioni non più
        monitorate. Uno stop loss su un'altra posizione poteva non essere
        visto per quei 45 minuti.

        Ora: il timeout è in SECONDI, uno stop loss non attende affatto, e il
        chiamante esegue questa funzione in un task separato (vedi main.py)
        così il loop continua a girare comunque.
        """
        r = CONFIG.risk
        if urgente and r.calm_ignora_su_stop_loss:
            return await self.vendi(token_mint, token_amount_raw)

        n = max(1, r.tranches_per_vendita)
        tranche_qty = token_amount_raw // n
        venduto_raw, ricavato = 0, 0.0
        ultima_firma, ultimo_errore = None, None

        for i in range(n):
            qty = tranche_qty if i < n - 1 else token_amount_raw - venduto_raw
            if qty <= 0:
                break
            deadline = time.time() + r.calm_timeout_sec

            while time.time() < deadline:
                calm = await market.calm_score(token_mint)
                slippage_ok = await market.slippage_stimato_ok(
                    token_mint, size_usd_totale / n, r.max_slippage_tranche_pct)
                if calm >= r.calm_soglia_vendita and slippage_ok:
                    break
                log.debug("⏸️ Tranche %d/%d in attesa (calm=%.0f, soglia=%.0f)",
                          i + 1, n, calm, r.calm_soglia_vendita)
                await asyncio.sleep(r.calm_check_sec)
            else:
                log.info("⏰ Timeout calm (%.0fs) su tranche %d/%d → vendo comunque",
                         r.calm_timeout_sec, i + 1, n)

            res = await self.vendi(token_mint, qty)
            if not res.ok:
                ultimo_errore = res.errore
                log.error("Tranche %d/%d fallita: %s", i + 1, n, res.errore)
                break  # niente retry qui: meglio riportare il parziale con precisione
            venduto_raw += res.unita_vendute_raw
            ricavato += res.output_amount
            ultima_firma = res.firma_tx

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
        out_stimato = float(quote.get("outAmount", 0))

        if self.paper_mode:
            self._log_paper_trade(quote)
            return SwapResult(
                ok=True, firma_tx="PAPER-" + str(int(time.time() * 1000)),
                input_amount=input_amount, output_amount=out_stimato, simulato=True,
            )

        r_cfg = CONFIG.risk
        body = {
            "quoteResponse": quote,
            "userPublicKey": str(self.keypair.pubkey()),
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
        }
        if r_cfg.usa_dynamic_slippage:
            body["dynamicSlippage"] = True
        if r_cfg.usa_jito:
            # Jupiter inserisce l'istruzione di tip verso un tip account Jito:
            # senza tip il bundle non viene nemmeno considerato.
            body["prioritizationFeeLamports"] = {"jitoTipLamports": CONFIG.api.jito_tip_lamports}
        else:
            body["prioritizationFeeLamports"] = {
                "priorityLevelWithMaxLamports": {
                    "priorityLevel": "high",
                    "maxLamports": r_cfg.priority_fee_microlamports,
                }
            }

        try:
            async with self.session.post(
                CONFIG.api.jupiter_swap_url, json=body,
                timeout=aiohttp.ClientTimeout(total=12)
            ) as r:
                if r.status != 200:
                    return SwapResult(ok=False, errore=f"swap build HTTP {r.status}: {(await r.text())[:160]}")
                swap_data = await r.json()

            if swap_data.get("simulationError"):
                return SwapResult(ok=False, errore=f"simulazione fallita: {swap_data['simulationError']}")

            raw_tx = base64.b64decode(swap_data["swapTransaction"])
            tx = VersionedTransaction.from_bytes(raw_tx)
            tx_firmata = VersionedTransaction(tx.message, [self.keypair])
            # La firma è intrinseca alla transazione firmata: identica a
            # prescindere dal canale di invio (Jito o RPC), quindi è l'unico
            # identificatore affidabile per interrogare la conferma. Il
            # "result" di sendBundle è l'ID del bundle, NON la firma.
            firma = str(tx_firmata.signatures[0])

            if not await self._invia_tx(bytes(tx_firmata)):
                return SwapResult(ok=False, errore="invio transazione fallito")

            esito = await self._conferma_e_leggi_esito(
                firma, str(self.keypair.pubkey()), mint_atteso, is_buy)

            if esito is None:
                # La conferma non è arrivata in tempo, ma la transazione può
                # atterrare dopo. Prima di dichiarare fallimento — e quindi di
                # lasciare eventuali token reali fuori dal monitoraggio —
                # rileggo il saldo on-chain.
                if is_buy and mint_atteso:
                    return await self._recupera_posizione_da_saldo(firma, mint_atteso, input_amount)
                return SwapResult(ok=False, firma_tx=firma,
                                  errore="transazione non confermata entro il timeout (o fallita on-chain)")

            out_reale = esito["amount"]
            if out_reale <= 0:
                return SwapResult(ok=False, firma_tx=firma,
                                  errore="importo ricevuto nullo/negativo dopo conferma")

            log.info("✅ Swap confermato %s (atteso≈%s, reale=%s)", firma[:16], out_stimato, out_reale)
            return SwapResult(ok=True, firma_tx=firma,
                              input_amount=input_amount, output_amount=float(out_reale))
        except Exception as e:
            log.error("Errore esecuzione swap: %s", e)
            return SwapResult(ok=False, errore=str(e))

    async def _recupera_posizione_da_saldo(self, firma: str, mint: str,
                                           input_amount: float) -> SwapResult:
        """Ultimo tentativo dopo un timeout di conferma su un ACQUISTO: se i
        token sono nel wallet, la transazione è atterrata comunque e la
        posizione va registrata. Senza questo si accumulano token reali che
        nessuno protegge con stop loss o ladder."""
        await asyncio.sleep(3)
        saldo = await self.saldo_token_raw(mint)
        if saldo > 0:
            log.warning("🔁 Conferma tardiva su %s: trovati %d token nel wallet, "
                        "registro la posizione (firma %s)", mint[:8], saldo, firma[:16])
            return SwapResult(ok=True, firma_tx=firma, input_amount=input_amount,
                              output_amount=float(saldo), recuperato_da_saldo=True)
        return SwapResult(ok=False, firma_tx=firma,
                          errore="transazione non confermata e nessun token nel wallet")

    # ---------------- CONFERMA E LETTURA ESITO ----------------

    async def _conferma_e_leggi_esito(self, firma: str, wallet_pubkey: str,
                                      mint_atteso: str | None, is_buy: bool) -> dict | None:
        deadline = time.time() + CONFIG.risk.timeout_conferma_sec
        while time.time() < deadline:
            try:
                body = {"jsonrpc": "2.0", "id": 1, "method": "getSignatureStatuses",
                        "params": [[firma], {"searchTransactionHistory": True}]}
                async with self.session.post(
                    CONFIG.api.rpc_url, json=body, timeout=aiohttp.ClientTimeout(total=8)
                ) as r:
                    data = await r.json()
                stato = ((data.get("result") or {}).get("value") or [None])[0]
                if stato:
                    if stato.get("err"):
                        log.error("❌ Transazione fallita on-chain (%s): %s", firma[:16], stato["err"])
                        return None
                    if stato.get("confirmationStatus") in ("confirmed", "finalized"):
                        return await self._leggi_esito_tx(firma, wallet_pubkey, mint_atteso, is_buy)
            except Exception as e:
                log.debug("Errore poll conferma %s: %s", firma[:16], e)
            await asyncio.sleep(CONFIG.risk.poll_conferma_sec)
        log.error("⏰ Timeout conferma %s dopo %.0fs", firma[:16], CONFIG.risk.timeout_conferma_sec)
        return None

    async def _leggi_esito_tx(self, firma: str, wallet_pubkey: str,
                              mint_atteso: str | None, is_buy: bool) -> dict | None:
        body = {"jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                "params": [firma, {"encoding": "jsonParsed",
                                   "maxSupportedTransactionVersion": 0}]}
        try:
            async with self.session.post(
                CONFIG.api.rpc_url, json=body, timeout=aiohttp.ClientTimeout(total=12)
            ) as r:
                data = await r.json()
        except Exception as e:
            log.error("Errore lettura esito tx %s: %s", firma[:16], e)
            return None

        tx = data.get("result")
        if not tx or not tx.get("meta"):
            return None
        meta = tx["meta"]

        if is_buy:
            pre = sum(int(b["uiTokenAmount"]["amount"])
                      for b in (meta.get("preTokenBalances") or [])
                      if b.get("mint") == mint_atteso and b.get("owner") == wallet_pubkey)
            post = sum(int(b["uiTokenAmount"]["amount"])
                       for b in (meta.get("postTokenBalances") or [])
                       if b.get("mint") == mint_atteso and b.get("owner") == wallet_pubkey)
            return {"amount": post - pre}

        # Vendita: interessa il delta di SOL (lamports) del wallet
        try:
            keys = tx["transaction"]["message"]["accountKeys"]
            idx = next((i for i, k in enumerate(keys)
                        if (k.get("pubkey") if isinstance(k, dict) else k) == wallet_pubkey), None)
            if idx is None:
                return None
            return {"amount": meta["postBalances"][idx] - meta["preBalances"][idx]}
        except (KeyError, IndexError, TypeError) as e:
            log.error("Errore parsing esito vendita %s: %s", firma[:16], e)
            return None

    # ---------------- INVIO (Jito con fallback RPC) ----------------

    async def _invia_via_jito(self, tx_bytes: bytes) -> bool:
        """Invia la transazione a un Jito Block Engine invece che al solo RPC.

        L'encoding DEVE essere dichiarato: la documentazione Jito specifica che
        il default di sendBundle è base58. La versione precedente inviava
        base64 senza dichiararlo, quindi ogni bundle veniva rifiutato e il bot
        ricadeva silenziosamente sull'RPC normale ad ogni singolo trade.
        """
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "sendBundle",
            "params": [[base64.b64encode(tx_bytes).decode()], {"encoding": "base64"}],
        }
        try:
            async with self.session.post(
                CONFIG.api.jito_block_engine_url, json=body,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status != 200:
                    log.warning("Jito HTTP %s → fallback RPC", r.status)
                    return False
                data = await r.json()
                if "error" in data:
                    log.warning("Jito error (%s) → fallback RPC", data["error"])
                    return False
                return True
        except Exception as e:
            log.warning("Jito non disponibile (%s) → fallback RPC", e)
            return False

    async def _invia_tx(self, tx_bytes: bytes) -> bool:
        if CONFIG.risk.usa_jito and await self._invia_via_jito(tx_bytes):
            log.info("⚡ Inviato via Jito bundle")
            return True

        body = {
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [base64.b64encode(tx_bytes).decode(),
                       {"encoding": "base64", "skipPreflight": True, "maxRetries": 3}],
        }
        try:
            async with self.session.post(
                CONFIG.api.rpc_url, json=body, timeout=aiohttp.ClientTimeout(total=12)
            ) as r:
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
            f.write(json.dumps({"ts": time.time(),
                                "inputMint": quote.get("inputMint"),
                                "outputMint": quote.get("outputMint"),
                                "inAmount": quote.get("inAmount"),
                                "outAmount": quote.get("outAmount"),
                                "priceImpactPct": quote.get("priceImpactPct")}) + "\n")
