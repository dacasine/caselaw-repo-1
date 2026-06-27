"""Legal decision translator — cache-backed, on-demand translation.

Translates Swiss court decisions between DE/FR/IT/EN using Gemini 2.5 Flash.
Stores translations in decision_translations for infinite cache.
"""
from __future__ import annotations

import logging
import time

import psycopg

from search_stack.parag.openrouter_client import OpenRouterClient

log = logging.getLogger("translator")

TRANSLATION_MODEL = "google/gemini-2.5-flash"

LANG_NAMES = {"de": "allemand", "fr": "français", "it": "italien", "en": "anglais"}

SYSTEM_PROMPT = """Tu es un traducteur juridique professionnel spécialisé en droit suisse.
Tu traduis des arrêts de tribunaux suisses avec une précision terminologique absolue.

RÈGLES IMPÉRATIVES :
1. Utilise la terminologie juridique officielle de la langue cible (pas de traduction littérale).
   Exemples : Beschwerde in Zivilsachen = recours en matière civile (pas "plainte civile")
   Bundesgericht = Tribunal fédéral, Obergericht = Tribunal cantonal / Cour supérieure
2. Conserve les références légales dans leur forme officielle :
   - Articles de loi : garde la notation originale (art. 41 OR / art. 41 CO selon la langue cible)
   - Abréviations de lois : utilise l'abréviation officielle dans la langue cible
     (OR→CO, ZGB→CC, StGB→CP, SchKG→LP, BGG→LTF, ZPO→CPC, StPO→CPP)
3. Les références BGE/ATF/DTF restent en "BGE" (convention de citation standard).
4. Conserve la structure exacte du texte (paragraphes, numérotation, dispositif).
5. Ne résume pas, ne coupe pas, ne commente pas — traduis intégralement.
6. Si un passage est déjà dans la langue cible, garde-le tel quel.
"""


def get_cached_translation(
    conn: psycopg.Connection,
    decision_id: str,
    target_lang: str,
) -> dict | None:
    """Check if translation already exists in cache."""
    row = conn.execute(
        "SELECT full_text, regeste, title, source_lang, model, created_at "
        "FROM decision_translations WHERE decision_id = %s AND target_lang = %s",
        (decision_id, target_lang),
    ).fetchone()
    if not row:
        return None
    return {
        "decision_id": decision_id,
        "target_lang": target_lang,
        "full_text": row[0],
        "regeste": row[1],
        "title": row[2],
        "source_lang": row[3],
        "model": row[4],
        "created_at": str(row[5]),
        "cached": True,
    }


def translate_decision(
    conn: psycopg.Connection,
    decision_id: str,
    target_lang: str,
    *,
    client: OpenRouterClient | None = None,
) -> dict:
    """Translate a decision to target_lang. Uses cache if available."""
    # Check cache
    cached = get_cached_translation(conn, decision_id, target_lang)
    if cached:
        return cached

    # Fetch source decision
    row = conn.execute(
        "SELECT language, full_text, regeste, title FROM decisions WHERE decision_id = %s",
        (decision_id,),
    ).fetchone()
    if not row:
        return {"error": f"Decision {decision_id} not found"}

    source_lang, full_text, regeste, title = row

    if source_lang == target_lang:
        return {"error": f"Decision is already in {target_lang}"}

    if not full_text:
        return {"error": "Decision has no full_text"}

    # Create client if not provided
    if client is None:
        client = OpenRouterClient(
            model=TRANSLATION_MODEL,
            fallback_model=TRANSLATION_MODEL,
            rate_limit_per_minute=30,
        )

    source_name = LANG_NAMES.get(source_lang, source_lang)
    target_name = LANG_NAMES.get(target_lang, target_lang)

    # Translate full_text
    t0 = time.monotonic()
    user_prompt = (
        f"Traduis intégralement cet arrêt du {source_name} vers le {target_name}.\n\n"
        f"{full_text}"
    )
    resp = client.chat(
        system=SYSTEM_PROMPT,
        user=user_prompt,
        max_tokens=65000,
        temperature=0.1,
    )
    translated_text = resp.content.strip()
    total_tokens = resp.usage.get("prompt_tokens", 0) + resp.usage.get("completion_tokens", 0)
    input_cost = resp.usage.get("prompt_tokens", 0) * 0.15 / 1_000_000
    output_cost = resp.usage.get("completion_tokens", 0) * 0.60 / 1_000_000
    cost = input_cost + output_cost

    # Translate regeste if present
    translated_regeste = None
    if regeste:
        resp2 = client.chat(
            system=SYSTEM_PROMPT,
            user=f"Traduis ce regeste du {source_name} vers le {target_name}.\n\n{regeste}",
            max_tokens=2000,
            temperature=0.1,
        )
        translated_regeste = resp2.content.strip()
        total_tokens += resp2.usage.get("prompt_tokens", 0) + resp2.usage.get("completion_tokens", 0)
        cost += resp2.usage.get("prompt_tokens", 0) * 0.15 / 1_000_000
        cost += resp2.usage.get("completion_tokens", 0) * 0.60 / 1_000_000

    # Translate title if present
    translated_title = None
    if title:
        resp3 = client.chat(
            system=SYSTEM_PROMPT,
            user=f"Traduis ce titre du {source_name} vers le {target_name} (une seule ligne).\n\n{title}",
            max_tokens=200,
            temperature=0.1,
        )
        translated_title = resp3.content.strip()

    latency = time.monotonic() - t0

    # Store in cache
    conn.execute(
        """INSERT INTO decision_translations
            (decision_id, target_lang, source_lang, full_text, regeste, title,
             model, token_count, cost_usd, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
           ON CONFLICT (decision_id, target_lang) DO UPDATE SET
            full_text = EXCLUDED.full_text, regeste = EXCLUDED.regeste,
            title = EXCLUDED.title, model = EXCLUDED.model,
            token_count = EXCLUDED.token_count, cost_usd = EXCLUDED.cost_usd,
            created_at = now()""",
        (decision_id, target_lang, source_lang, translated_text,
         translated_regeste, translated_title,
         TRANSLATION_MODEL, total_tokens, round(cost, 5)),
    )
    conn.commit()

    log.info("Translated %s %s→%s: %d tokens, $%.4f, %.1fs",
             decision_id, source_lang, target_lang, total_tokens, cost, latency)

    return {
        "decision_id": decision_id,
        "target_lang": target_lang,
        "source_lang": source_lang,
        "full_text": translated_text,
        "regeste": translated_regeste,
        "title": translated_title,
        "model": TRANSLATION_MODEL,
        "token_count": total_tokens,
        "cost_usd": round(cost, 5),
        "cached": False,
    }
