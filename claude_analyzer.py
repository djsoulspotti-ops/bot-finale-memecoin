"""
claude_analyzer.py — Analisi qualitativa dei token con l'API Anthropic.

RUOLO DI CLAUDE NEL BOT:
Claude NON esegue trade e NON "fa girare" il bot: il bot gira sul tuo VPS.
Claude viene chiamato via API come "analista": riceve i dati del token
(metriche, risultati dei filtri, metadati) e restituisce un punteggio 0-100
con motivazione. Il bot apre una posizione solo se score >= soglia.

Costo: ~0.003-0.01 USD per analisi con Claude Sonnet. Con i filtri hard
a monte, arrivano a Claude solo pochi token al giorno → costo trascurabile.
"""

import json
import logging

import aiohttp

from config import CONFIG
from filters import FilterResult
from scanner import TokenCandidate

log = logging.getLogger("claude")

SYSTEM_PROMPT = """Sei un analista quantitativo specializzato in token Solana a bassa capitalizzazione.
Ricevi i dati di un token appena listato che ha già superato filtri anti-rug automatici.
Il tuo compito: valutare la probabilità che il token abbia momentum positivo nelle prossime 1-2 ore.

Valuta questi fattori:
1. Rapporto volume/liquidità (sano se 0.2-2.0 negli ultimi 5 min)
2. Rapporto buy/sell (sano se > 1.2)
3. Nome/simbolo: coerenza con meta/trend attuali, red flag (impersonificazione, scam noti, caratteri strani)
4. Market cap vs liquidità (sospetto se mcap/liq > 20)
5. Distribuzione holder e score RugCheck

Rispondi SOLO con JSON valido, senza markdown, nel formato:
{"score": <0-100>, "decisione": "COMPRA"|"SCARTA", "motivazione": "<max 200 caratteri>", "rischi": ["<rischio1>", "<rischio2>"]}

Sii severo: la maggior parte dei token merita SCARTA. Score >= 70 solo con segnali chiaramente positivi."""


class ClaudeAnalyzer:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def analizza(self, c: TokenCandidate, f: FilterResult) -> dict:
        """Restituisce {"score": int, "decisione": str, "motivazione": str, "rischi": list}."""
        payload_token = {
            "symbol": c.symbol,
            "name": c.name,
            "dex": c.dex,
            "eta_pool_minuti": round(c.eta_minuti, 1),
            "liquidita_usd": c.liquidity_usd,
            "volume_5m_usd": c.volume_5m_usd,
            "market_cap_usd": c.market_cap_usd,
            "prezzo_usd": c.price_usd,
            "variazione_prezzo_5m_pct": c.price_change_5m,
            "buys_5m": c.buys_5m,
            "sells_5m": c.sells_5m,
            "rugcheck_score": f.rugcheck_score,
            "top10_holder_pct": f.top10_holder_pct,
            "mint_revocato": f.mint_revocato,
            "lp_bloccata": f.lp_bloccata,
        }

        body = {
            "model": CONFIG.api.claude_model,
            "max_tokens": 500,
            "system": SYSTEM_PROMPT,
            "messages": [
                {"role": "user", "content": f"Analizza questo token:\n{json.dumps(payload_token, indent=2)}"}
            ],
        }
        headers = {
            "x-api-key": CONFIG.api.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        try:
            async with self.session.post(
                "https://api.anthropic.com/v1/messages",
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status != 200:
                    log.error("Anthropic API HTTP %s: %s", r.status, await r.text())
                    return self._fallback("errore API")
                data = await r.json()
        except Exception as e:
            log.error("Errore chiamata Anthropic: %s", e)
            return self._fallback(str(e))

        try:
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            text = text.replace("```json", "").replace("```", "").strip()
            result = json.loads(text)
            log.info("🤖 Claude su %s → score=%s (%s)", c.symbol, result.get("score"), result.get("decisione"))
            return result
        except (json.JSONDecodeError, KeyError) as e:
            log.error("Risposta Claude non parsabile: %s", e)
            return self._fallback("parsing fallito")

    @staticmethod
    def _fallback(motivo: str) -> dict:
        """In caso di errore API: MAI comprare alla cieca. Fail-safe = SCARTA."""
        return {"score": 0, "decisione": "SCARTA", "motivazione": f"fallback: {motivo}", "rischi": ["analisi non disponibile"]}
