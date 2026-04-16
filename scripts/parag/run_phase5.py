"""Run Phase 5 enrichment over a court (default: bge / ATF only).

Usage:
    .venv/bin/python scripts/parag/run_phase5.py \\
        --court bge --workers 8 --rate 120 \\
        --model google/gemini-2.0-flash-001
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from db_schema_parag import DEFAULT_PARAG_DB, init_parag_schema
from search_stack.parag.citation_resolver import CitationResolver
from search_stack.parag.openrouter_client import (
    DEFAULT_MODEL,
    DEFAULT_FALLBACK_MODEL,
    OpenRouterClient,
)
from search_stack.parag.phase5_worker import Phase5Row, run_many


DEFAULT_SOURCE_DB = Path.home() / ".swiss-caselaw" / "decisions.db"


def fetch_rows(
    src_db: Path, court: str, limit: int | None,
    only_already_enriched: bool, parag_db: Path,
) -> list[Phase5Row]:
    """If `only_already_enriched` is True, restrict to decisions that
    already have at least one chunk in parag_chunks.db — avoids paying
    LLM calls on arrêts we haven't SAC-enriched yet."""
    conn = sqlite3.connect(str(src_db))
    conn.row_factory = sqlite3.Row
    eligible_ids: set[str] | None = None
    if only_already_enriched:
        pconn = sqlite3.connect(str(parag_db))
        eligible_ids = {
            r[0] for r in pconn.execute(
                "SELECT DISTINCT decision_id FROM chunks"
            ).fetchall()
        }
        pconn.close()
    sql = (
        "SELECT decision_id, court, language, decision_date, chamber, "
        "COALESCE(regeste,'') AS regeste, full_text "
        "FROM decisions WHERE court = ? AND full_text IS NOT NULL "
        "AND length(full_text) > 500 ORDER BY decision_id"
    )
    out: list[Phase5Row] = []
    for r in conn.execute(sql, (court,)):
        if eligible_ids is not None and r["decision_id"] not in eligible_ids:
            continue
        out.append(Phase5Row(
            decision_id=r["decision_id"],
            court=r["court"],
            language=r["language"],
            date_iso=r["decision_date"] or "",
            chamber=r["chamber"],
            regeste=r["regeste"],
            full_text=r["full_text"],
        ))
        if limit and len(out) >= limit:
            break
    conn.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--court", default="bge")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--rate", type=int, default=120,
                    help="LLM requests per minute (client-side cap)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--fallback", default=DEFAULT_FALLBACK_MODEL)
    ap.add_argument("--light", action="store_true",
                    help="Use SYSTEM_PROMPT_LIGHT (for BGer / unpublished)")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only-sac-enriched", action="store_true", default=True,
                    help="Only process decisions that have SAC chunks "
                         "(default: True — don't pay LLM for non-chunked arrêts)")
    ap.add_argument("--src-db", type=Path, default=DEFAULT_SOURCE_DB)
    ap.add_argument("--parag-db", type=Path, default=DEFAULT_PARAG_DB)
    args = ap.parse_args()

    print(f"Source DB : {args.src_db}", file=sys.stderr)
    print(f"Parag DB  : {args.parag_db}", file=sys.stderr)
    print(f"Court     : {args.court}  workers={args.workers}  rate={args.rate}/min  "
          f"force={args.force}", file=sys.stderr)
    print(f"Model     : {args.model}", file=sys.stderr)
    print(f"Fallback  : {args.fallback}", file=sys.stderr)
    print(f"Variant   : {'LIGHT' if args.light else 'FULL'}", file=sys.stderr)

    parag_conn = init_parag_schema(args.parag_db)
    rows = fetch_rows(
        args.src_db, args.court, args.limit,
        only_already_enriched=args.only_sac_enriched,
        parag_db=args.parag_db,
    )
    print(f"Loaded {len(rows)} decisions", file=sys.stderr)
    if not rows:
        print("Nothing to do.", file=sys.stderr)
        return

    client = OpenRouterClient(
        model=args.model,
        fallback_model=args.fallback,
        rate_limit_per_minute=args.rate,
    )
    resolver = CitationResolver(
        statutes_db_path=Path.home() / ".swiss-caselaw" / "statutes.db",
        cantonal_db_path=Path.home() / ".swiss-caselaw" / "cantonal_laws.db",
    )

    agg = run_many(
        rows,
        parag_conn=parag_conn, client=client, resolver=resolver,
        n_workers=args.workers,
        progress_every=max(10, len(rows) // 40),
        use_light_prompt=args.light,
        force=args.force,
    )
    parag_conn.close()

    print("\n════ FINAL ════")
    print(json.dumps(agg, indent=2, default=str))


if __name__ == "__main__":
    main()
