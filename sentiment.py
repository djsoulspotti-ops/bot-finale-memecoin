"""
sentiment.py — Sentiment analysis social per il token candidato.

Nota di onestà tecnica: le API ufficiali di X/Twitter sono a pagamento
(da ~200 $/mese) e Telegram/Discord non espongono ricerche pubbliche.
La soluzione qui usa due fonti realmente accessibili:

  1. SEGNALI STRUTTURALI (DexScreener): presenza di sito web, Twitter,
     Telegram nel token profile, boost attivi, immagine/header curati.
     Un token serio investe nella propria presenza; un rug usa template vuoti.

  2. WEB SEARCH VIA CLAUDE: la chiamata all'API Anthropic abilita il tool
     server-side `web_search`. Claude cerca "$SYMBOL solana" sul web
     (news, aggregatori, thread indicizzati) e valuta il sentiment:
     hype organico vs shill coordinato vs silenzio totale.

Output: {"sentiment_score": 0-100, "hype_type": "organico|shill|assente",
         "note": "..."} — usato come gate d'ingresso (soglia in config).
"""

import json
import logging

import aiohttp

from config import CONFIG
from scanner import TokenCandidate

log = logging.getLogger("sentiment")

SENTIMENT_PROMPT = """Sei un analista di sentiment per token Solana appena lanciati.
Usa la ricerca web per cercare informazioni recenti sul token indicato (simbolo + "solana").
Valuta SOLO segnali delle ultime 24-48 ore.

Classifica:
- Hype ORGANICO: menzioni spontanee, community reale, meme che circolano → score alto
- SHILL coordinato: post identici, bot, promesse di guadagno, "1000x gem" → score basso (è un red flag, non un segnale positivo)
- ASSENTE: nessuna traccia social → score basso

Rispondi SOLO con JSON valido, senza markdown:
{"sentiment_score": <0-100>, "hype_type": "organico"|"shill"|"assente", "note": "<max 150 caratteri>"}

Sii severo: shill coordinato e assenza totale meritano score < 40."""


class SentimentAnalyzer:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    # ---------- 1. Segnali strutturali (gratis, immediati) ----------

    def segnali_strutturali(self, c: TokenCandidate) -> dict:
        info = (c.raw.get("info") or {})
        socials = info.get("socials") or []
        websites = info.get("websites") or []
        tipi = {s.get("type") for s in socials}

        punti = 0
        punti += 25 if "twitter" in tipi else 0
        punti += 20 if "telegram" in tipi else 0
        punti += 20 if websites else 0
        punti += 15 if info.get("imageUrl") else 0
        punti += 20 if c.raw.get("boosts") else 0

        return {
            "score_strutturale": punti,          # 0-100
            "ha_twitter": "twitter" in tipi,
            "ha_telegram": "telegram" in tipi,
            "ha_sito": bool(websites),
        }

    # ---------- 2. Web sentiment via Claude + web_search ----------

    async def analizza(self, c: TokenCandidate) -> dict:
        strutturali = self.segnali_strutturali(c)

        # Gate rapido: zero presenza social = inutile cercare sul web
        if strutturali["score_strutturale"] < 20:
            return {"sentiment_score": 10, "hype_type": "assente",
                    "note": "nessuna presenza social strutturata", **strutturali}

        body = {
            "model": CONFIG.api.claude_model,
            "max_tokens": 800,
            "system": SENTIMENT_PROMPT,
            "messages": [{
                "role": "user",
                "content": (
                    f"Token: {c.name} (${c.symbol}) su Solana, mint {c.mint}. "
                    f"Segnali strutturali: {json.dumps(strutturali)}. "
                    f"Cerca sul web il sentiment recente e valuta."
                ),
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
                "https://api.anthropic.com/v1/messages",
                json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=90),
            ) as r:
                if r.status != 200:
                    log.error("Sentiment API HTTP %s: %s", r.status, await r.text())
                    return self._fallback(strutturali)
                data = await r.json()
        except Exception as e:
            log.error("Errore sentiment: %s", e)
            return self._fallback(strutturali)

        # La risposta con tool use contiene blocchi misti: prendi solo il testo finale
        try:
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            text = text.replace("```json", "").replace("```", "").strip()
            # Estrai l'ultimo oggetto JSON nel testo
            start = text.rfind("{")
            result = json.loads(text[start:])
            result.update(strutturali)
            log.info("📣 Sentiment %s → %s (%s)", c.symbol, result.get("sentiment_score"), result.get("hype_type"))
            return result
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            log.error("Sentiment non parsabile: %s", e)
            return self._fallback(strutturali)

    @staticmethod
    def _fallback(strutturali: dict) -> dict:
        """Errore = prudenza: score basso, il gate d'ingresso bloccherà."""
        return {"sentiment_score": 0, "hype_type": "assente",
                "note": "analisi non disponibile (fallback)", **strutturali}
