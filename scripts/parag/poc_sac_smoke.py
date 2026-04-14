"""End-to-end smoke test: parse 2 BGE + 1 BGer, generate a summary header
via synthetic.new for the first 2 considérants of each, print before / after.

Usage:
    PYTHONPATH=. python3 scripts/parag/poc_sac_smoke.py

Requires SYNTHETIC_API_KEY in environment or .env.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from search_stack.parag.llm_client import SyntheticClient
from search_stack.parag.parsers import parse
from search_stack.parag.parsers._common import clean_chunk_text


DB_PATH = Path.home() / ".swiss-caselaw" / "decisions.db"


SYSTEM_PROMPT = (
    "Tu es un juriste spécialiste du droit suisse. Pour chaque considérant "
    "fourni, génère UNE SEULE phrase concise (15-30 mots) en {lang} qui "
    "contextualise ce considérant dans l'arrêt : mentionne la question "
    "juridique traitée, la partie concernée et/ou la conclusion du considérant. "
    "Réponds UNIQUEMENT par la phrase demandée, sans préambule, sans "
    "guillemets, sans numérotation."
)


USER_TEMPLATE = """Arrêt : {decision_id} (cour : {court}, langue : {language})
Regeste (si disponible) : {regeste}

Considérant {number} (texte brut) :
{text}"""


def pick_samples(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    # 2 random BGE + 1 random BGer, must have enough text
    rows: list[tuple[str, str, str, str]] = []
    for court, n in [("bge", 2), ("bger", 1)]:
        rows.extend(
            conn.execute(
                "SELECT decision_id, court, language, full_text, regeste "
                "FROM decisions WHERE court=? AND length(full_text)>10000 "
                "ORDER BY RANDOM() LIMIT ?",
                (court, n),
            ).fetchall()
        )
    return rows


def main() -> None:
    client = SyntheticClient(rate_limit_per_minute=20)
    conn = sqlite3.connect(str(DB_PATH))

    samples = pick_samples(conn)
    total_tokens_in = total_tokens_out = 0
    total_latency = 0.0
    n_calls = 0

    for did, court, lang, txt, regeste in samples:
        parsed = parse(did, court, lang, txt)
        if not parsed.considerants:
            print(f"\n[{did}] no considérants found, skipping")
            continue
        lang_label = {"de": "allemand", "fr": "français", "it": "italien"}.get(lang, "français")
        print(f"\n══════════ {did} [{lang}] ({court}) ══════════")
        print(f"  {len(parsed.considerants)} considérants detected "
              f"(parser={parsed.parser_name})")

        for c in parsed.considerants[:2]:
            raw = txt[c.start:c.end]
            cleaned = clean_chunk_text(raw)
            user = USER_TEMPLATE.format(
                decision_id=did,
                court=court,
                language=lang_label,
                regeste=(regeste or "").strip()[:600] or "(non disponible)",
                number=c.number,
                text=cleaned[:2000],
            )
            sys_prompt = SYSTEM_PROMPT.replace("{lang}", lang_label)
            print(f"\n  ── considérant {c.number} (depth={c.depth}, len={c.length}) ──")
            print(f"    FIRST 160 CHARS: {cleaned[:160]!r}")

            try:
                resp = client.chat(
                    system=sys_prompt,
                    user=user,
                    max_tokens=2000,
                    temperature=0.2,
                )
            except Exception as exc:
                print(f"    ERROR: {exc}")
                continue

            total_tokens_in += resp.usage.get("prompt_tokens", 0)
            total_tokens_out += resp.usage.get("completion_tokens", 0)
            total_latency += resp.latency_s
            n_calls += 1

            print(f"    SUMMARY  : {resp.content}")
            print(f"    model={resp.model} fb={resp.fallback_used} "
                  f"latency={resp.latency_s:.1f}s "
                  f"tokens={resp.usage.get('prompt_tokens','?')}→"
                  f"{resp.usage.get('completion_tokens','?')}")

    print(f"\n════ TOTALS ════")
    print(f"  calls       : {n_calls}")
    print(f"  latency sum : {total_latency:.1f}s  (avg {total_latency/max(n_calls,1):.1f}s)")
    print(f"  tokens in   : {total_tokens_in}")
    print(f"  tokens out  : {total_tokens_out}")


if __name__ == "__main__":
    main()
