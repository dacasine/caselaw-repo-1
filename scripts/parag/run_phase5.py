"""Run Phase 5 enrichment over a court (default: bge / ATF only).

Usage:
    .venv/bin/python scripts/parag/run_phase5.py \
        --court bge --workers 8 --rate 120 \
        --model google/gemini-2.0-flash-001

Reads from Postgres (CASELAW_PG_URL).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from search_stack.parag.citation_resolver import CitationResolver
from search_stack.parag.openrouter_client import (
    DEFAULT_MODEL,
    DEFAULT_FALLBACK_MODEL,
    OpenRouterClient,
)
from search_stack.parag.pg_conn import get_pool, get_pg_url
from search_stack.parag.phase5_worker import Phase5Row, run_many


def fetch_rows(
    pool, court: str, limit: int | None,
    only_already_enriched: bool,
) -> list[Phase5Row]:
    with pool.connection() as conn:
        # Pre-filter: only decisions that have SAC chunks AND are not yet
        # successfully enriched in Phase 5. This avoids loading + skipping
        # hundreds of thousands of already-done decisions.
        filters = ["d.court = %s", "d.full_text IS NOT NULL", "length(d.full_text) > 500"]
        params: list = [court]
        if only_already_enriched:
            filters.append(
                "EXISTS (SELECT 1 FROM chunks c WHERE c.decision_id = d.decision_id)"
            )
        filters.append(
            "NOT EXISTS (SELECT 1 FROM decision_enrichment de "
            "WHERE de.decision_id = d.decision_id AND ("
            "de.status IN ('ok','schema_invalid') OR "
            "(de.status = 'error' AND de.error_message LIKE '%%403%%')))"
        )
        where = " AND ".join(filters)
        sql = (
            "SELECT d.decision_id, d.court, d.language, d.decision_date, d.chamber, "
            f"COALESCE(d.regeste,'') AS regeste, d.full_text "
            f"FROM decisions d WHERE {where} ORDER BY d.decision_id"
        )
        if limit:
            sql += " LIMIT %s"
            params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        out = [
            Phase5Row(
                decision_id=r[0], court=r[1], language=r[2],
                date_iso=str(r[3] or ""), chamber=r[4],
                regeste=r[5], full_text=r[6],
            )
            for r in rows
        ]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--court", default="bge")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--rate", type=int, default=120)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--fallback", default=DEFAULT_FALLBACK_MODEL)
    ap.add_argument("--light", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only-sac-enriched", action="store_true", default=True)
    args = ap.parse_args()

    pool = get_pool(min_size=2, max_size=args.workers + 4)
    print(f"Postgres  : {get_pg_url().split('@')[-1]}", file=sys.stderr)
    print(f"Court     : {args.court}  workers={args.workers}  rate={args.rate}/min  "
          f"force={args.force}", file=sys.stderr)
    print(f"Model     : {args.model}", file=sys.stderr)
    print(f"Variant   : {'LIGHT' if args.light else 'FULL'}", file=sys.stderr)

    rows = fetch_rows(pool, args.court, args.limit,
                      only_already_enriched=args.only_sac_enriched)
    print(f"Loaded {len(rows)} decisions", file=sys.stderr)
    if not rows:
        print("Nothing to do.", file=sys.stderr)
        return

    client = OpenRouterClient(
        model=args.model, fallback_model=args.fallback,
        rate_limit_per_minute=args.rate,
    )
    resolver = CitationResolver(
        statutes_db_path=Path.home() / ".swiss-caselaw" / "statutes.db",
        cantonal_db_path=Path.home() / ".swiss-caselaw" / "cantonal_laws.db",
    )

    agg = run_many(
        rows, pool=pool, client=client, resolver=resolver,
        n_workers=args.workers,
        progress_every=max(10, len(rows) // 40),
        use_light_prompt=args.light,
        force=args.force,
    )
    print("\n════ FINAL ════")
    print(json.dumps(agg, indent=2, default=str))


if __name__ == "__main__":
    main()
