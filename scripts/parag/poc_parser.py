"""POC runner: sample 10 BGE decisions, parse them, print a structural report.

Usage: python scripts/parag/poc_parser.py [--n 10] [--seed 42]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from search_stack.parag.parsers import parse

DEFAULT_DB = Path.home() / ".swiss-caselaw" / "decisions.db"


def sample_decisions(
    db_path: Path,
    court: str,
    n: int,
    min_len: int = 8000,
) -> list[tuple[str, str, str, str]]:
    """Returns rows of (decision_id, court, language, full_text)."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            """
            SELECT decision_id, court, language, full_text
            FROM decisions
            WHERE court=? AND full_text IS NOT NULL AND length(full_text) > ?
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (court, min_len, n),
        ).fetchall()
        return rows
    finally:
        conn.close()


def format_report(parsed_list: list) -> str:
    lines = []
    overall = {
        "n_decisions": len(parsed_list),
        "has_facts": 0,
        "has_considerations": 0,
        "has_dispositif": 0,
        "has_all_three": 0,
        "n_considerants_total": 0,
        "fallback_used": 0,
        "parsers_used": {},
    }

    for p in parsed_list:
        st = p.stats
        overall["has_facts"] += int(st["has_facts"])
        overall["has_considerations"] += int(st["has_considerations"])
        overall["has_dispositif"] += int(st["has_dispositif"])
        overall["has_all_three"] += int(
            st["has_facts"] and st["has_considerations"] and st["has_dispositif"]
        )
        overall["n_considerants_total"] += st["n_considerants_total"]
        overall["fallback_used"] += int(st["fallback_used"])
        overall["parsers_used"][st["parser"]] = overall["parsers_used"].get(st["parser"], 0) + 1

        sections_str = ", ".join(f"{s.type}@{s.start}" for s in p.sections) or "(none)"
        top_nums = [c.number for c in p.considerants if c.depth == 1]
        lines.append(
            f"\n─── {p.decision_id}  lang={p.language}  len={p.text_length}  parser={st['parser']} ───"
        )
        lines.append(f"  sections : {sections_str}")
        lines.append(
            f"  considérants : {st['n_considerants_top']} top-level "
            f"({', '.join(top_nums) if top_nums else '—'}), "
            f"{st['n_considerants_total']} total, max_depth={st['max_depth']}"
        )
        lines.append(f"  coverage: {st['considerant_coverage_ratio']:.1%} of full_text")

    lines.append("\n═══ SUMMARY ═══")
    n = overall["n_decisions"]
    lines.append(f"  {overall['has_facts']}/{n} decisions have 'facts' marker")
    lines.append(f"  {overall['has_considerations']}/{n} decisions have 'considerations' marker")
    lines.append(f"  {overall['has_dispositif']}/{n} decisions have 'dispositif' marker")
    lines.append(f"  {overall['has_all_three']}/{n} decisions have all three")
    lines.append(f"  {overall['n_considerants_total']} total considérants across all decisions")
    lines.append(f"  parsers used: {overall['parsers_used']}")
    lines.append(f"  fallback_used: {overall['fallback_used']}/{n}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--court", type=str, default="bge", help="court code to sample")
    ap.add_argument("--min-len", type=int, default=8000)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--out", type=Path, default=None, help="optional JSON dump path")
    args = ap.parse_args()

    samples = sample_decisions(args.db, args.court, args.n, args.min_len)
    if not samples:
        print(f"No samples found for court='{args.court}'. Check DB path and availability.")
        return

    parsed = [parse(did, court, lang, txt) for did, court, lang, txt in samples]
    print(format_report(parsed))

    if args.out:
        payload = [
            {
                "decision_id": p.decision_id,
                "language": p.language,
                "text_length": p.text_length,
                "sections": [
                    {"type": s.type, "start": s.start, "end": s.end, "marker": s.marker}
                    for s in p.sections
                ],
                "considerants": [
                    {
                        "number": c.number,
                        "start": c.start,
                        "end": c.end,
                        "depth": c.depth,
                        "lettered_subs": c.lettered_subs,
                    }
                    for c in p.considerants
                ],
                "stats": p.stats,
            }
            for p in parsed
        ]
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"\n→ JSON dump written to {args.out}")


if __name__ == "__main__":
    main()
