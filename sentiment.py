"""
sentiment.py — Sentiment social per il token candidato, 100% locale.

Nota di onestà tecnica: le API ufficiali di X/Twitter sono a pagamento
(da ~200 $/mese) e Telegram/Discord non espongono ricerche pubbliche.
Questa versione usa SOLO i segnali strutturali disponibili gratis via
DexScreener: presenza di sito web, Twitter, Telegram nel token profile,
boost attivi, immagine/header curati. Un token serio investe nella
propria presenza; un rug usa quasi sempre template vuoti.

Limite dichiarato: senza una ricerca web (prima demandata a Claude), non
si distingue più "hype organico" da "shill coordinato" — quel giudizio
qualitativo richiedeva letture di contenuto reale, non replicabile con
euristiche locali senza inventarsi un segnale che non esiste. Il gate
resta comunque utile: un token senza presenza social strutturata è quasi
sempre uno scarto a prescindere dal tipo di hype.

Output: {"sentiment_score": 0-100, "hype_type": "buona_presenza|presenza_parziale|assente",
         "note": "..."} — usato come gate d'ingresso (soglia in config).
"""

import logging

import aiohttp

from scanner import TokenCandidate

log = logging.getLogger("sentiment")


class SentimentAnalyzer:
    def __init__(self, session: aiohttp.ClientSession | None = None):
        # La sessione non serve più (nessuna chiamata di rete), ma si tiene
        # il parametro per non rompere la firma usata da main.py.
        self.session = session

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

    async def analizza(self, c: TokenCandidate) -> dict:
        """Async solo per compatibilità con l'interfaccia precedente (main.py
        fa `await` e `asyncio.gather` su questa chiamata insieme al filtro)."""
        s = self.segnali_strutturali(c)
        punti = s["score_strutturale"]

        if punti >= 60:
            hype_type, note = "buona_presenza", "presenza social strutturata solida"
        elif punti >= 20:
            hype_type, note = "presenza_parziale", "presenza social strutturata parziale"
        else:
            hype_type, note = "assente", "nessuna presenza social strutturata"

        log.info("📣 Sentiment %s → %d (%s)", c.symbol, punti, hype_type)
        return {"sentiment_score": punti, "hype_type": hype_type, "note": note, **s}
