"""
market_sentiment.py — Sentiment AGGREGATO del mercato memecoin (non del singolo token).

Differenza con sentiment.py:
  - sentiment.py    → "Questo specifico token ha hype organico o è shill?"
  - market_sentiment.py → "Il mercato memecoin nel suo complesso è in euforia,
                            normale, o in fase di panico/liquidazione?"

Perché serve: un token tecnicamente pulito che passa tutti i filtri si comporta
in modo molto diverso se lanciato durante un mercato in "risk-on" euforico
rispetto a un mercato in "risk-off" post-crollo (es. dopo un evento
macro negativo, un hack importante, o una cascata di rug pull mediatici).

Il regime di mercato NON blocca mai il bot del tutto (niente override totale):
modula il cluster_top_n e la size massima concessa. Un mercato in panico
riduce l'esposizione, non la azzera — resta comunque il risk manager a
decidere i singoli trade.

Uso tipico: chiamato una volta ogni ~30-60 minuti (non a ogni ciclo:
il regime di mercato cambia lentamente, e ogni chiamata costa una web search).
"""

import json
import logging
import time

import aiohttp

from config import CONFIG

log = logging.getLogger("market_sentiment")

SYSTEM = """Sei un analista di mercato per il settore memecoin su Solana e in generale crypto.
Usa la ricerca web per valutare il CLIMA GENERALE del mercato memecoin nelle ultime 24 ore,
NON un singolo token. Cerca notizie, volumi aggregati, eventi macro (BTC/SOL trend,
notizie regolatorie, hack, exploit di rilievo, sentiment generale su crypto twitter/X).

Classifica il regime in una di queste categorie:
- EUFORIA: volumi alti, molti lanci, FOMO diffuso, rischio di bolla/top locale
- NORMALE: attività ordinaria, nessun segnale estremo
- CAUTELA: segnali di raffreddamento, volumi in calo, incertezza
- PANICO: crollo diffuso, liquidazioni, eventi negativi maggiori (hack, regolamentazione avversa)

Rispondi SOLO con JSON valido, senza markdown:
{"regime": "EUFORIA"|"NORMALE"|"CAUTELA"|"PANICO",
 "score": <0-100, dove 50=normale, >70=euforia, <30=panico>,
 "note": "<max 200 caratteri sui fattori principali>",
 "eventi_rilevanti": ["<evento1>", "<evento2>"]}

Sii conservativo: se le informazioni sono scarse o ambigue, classifica come NORMALE con score 50."""


class MarketSentiment:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.cache: dict | None = None
        self.cache_ts: float = 0.0
        self.ttl_sec = 45 * 60  # ricalcola al massimo ogni 45 minuti

    async def regime(self, forza_refresh: bool = False) -> dict:
        """Ritorna il regime corrente, usando la cache se ancora valida."""
        if not forza_refresh and self.cache and (time.time() - self.cache_ts) < self.ttl_sec:
            return self.cache

        risultato = await self._interroga_claude()
        self.cache = risultato
        self.cache_ts = time.time()
        return risultato

    async def _interroga_claude(self) -> dict:
        body = {
            "model": CONFIG.api.claude_model,
            "max_tokens": 600,
            "system": SYSTEM,
            "messages": [{
                "role": "user",
                "content": "Valuta il regime di mercato memecoin/Solana attuale.",
            }],
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        }
        headers = {
            "x-api-key": CONFIG.api.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        try:
            async with self.session.post(
                "https://api.anthropic.com/v1/messages", json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=90),
            ) as r:
                if r.status != 200:
                    log.error("Market sentiment HTTP %s: %s", r.status, await r.text())
                    return self._fallback()
                data = await r.json()
        except Exception as e:
            log.error("Errore market sentiment: %s", e)
            return self._fallback()

        try:
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            text = text.replace("```json", "").replace("```", "").strip()
            start = text.rfind("{")
            result = json.loads(text[start:])
            log.info("🌡️ Regime di mercato: %s (score %s) — %s",
                     result.get("regime"), result.get("score"), result.get("note"))
            return result
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            log.error("Market sentiment non parsabile: %s", e)
            return self._fallback()

    @staticmethod
    def _fallback() -> dict:
        """Errore = prudenza: regime NORMALE, nessuna modulazione applicata."""
        return {"regime": "NORMALE", "score": 50, "note": "analisi non disponibile (fallback)", "eventi_rilevanti": []}

    # ---------------- MODULAZIONE (usata dal bot) ----------------

    @staticmethod
    def modulatori(regime_data: dict) -> dict:
        """
        Traduce il regime in moltiplicatori concreti per il bot.
        Non azzera mai l'operatività: PANICO riduce, non blocca.
        """
        regime = regime_data.get("regime", "NORMALE")
        mappa = {
            "EUFORIA":  {"cluster_top_n_delta": 0,  "size_mult": 0.90, "min_momentum_extra": 5,
                        "nota": "euforia = rischio di comprare il top locale: leggermente più prudente"},
            "NORMALE":  {"cluster_top_n_delta": 0,  "size_mult": 1.00, "min_momentum_extra": 0,
                        "nota": "nessuna modulazione"},
            "CAUTELA":  {"cluster_top_n_delta": -1, "size_mult": 0.75, "min_momentum_extra": 10,
                        "nota": "riduci esposizione ed esigi momentum più forte"},
            "PANICO":   {"cluster_top_n_delta": -1, "size_mult": 0.50, "min_momentum_extra": 20,
                        "nota": "esposizione dimezzata, solo segnali molto forti"},
        }
        return mappa.get(regime, mappa["NORMALE"])
