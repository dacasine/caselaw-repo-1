"""Full SAC run over a court (default: bge = ATF).

Usage:
    PYTHONPATH=. python3 scripts/parag/run_sac.py [--court bge] [--limit N]
                                                  [--workers 4] [--rate 30]
                                                  [--force]

Reads decisions from Postgres (CASELAW_PG_URL) and writes chunks back to Postgres.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from db_schema_parag import PROMPT_VERSION
from search_stack.parag.llm_client import SyntheticClient
from search_stack.parag.openrouter_client import OpenRouterClient
from search_stack.parag.pg_conn import get_pool, get_pg_url
from search_stack.parag.worker import DecisionRow, run_many


def fetch_rows(pool, court: str, limit: int | None, order: str = "id") -> list[DecisionRow]:
    order_sql = {
        "id": "ORDER BY decision_id",
        "random": "ORDER BY random()",
        "length": "ORDER BY length(full_text) DESC",
    }.get(order, "ORDER BY decision_id")
    q = (
        "SELECT decision_id, court, language, full_text, COALESCE(regeste,'') "
        "FROM decisions WHERE court = %s AND full_text IS NOT NULL "
        f"AND length(full_text) > 500 {order_sql}"
    )
    args: list = [court]
    if limit:
        q += " LIMIT %s"
        args.append(limit)
    with pool.connection() as conn:
        rows = conn.execute(q, args).fetchall()
    return [
        DecisionRow(decision_id=r[0], court=r[1], language=r[2],
                    full_text=r[3], regeste=r[4])
        for r in rows
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--court", default="bge")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rate", type=int, default=30)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--order", choices=["id", "random", "length"], default="id")
    ap.add_argument("--provider", choices=["synthetic", "openrouter"], default="openrouter")
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    pool = get_pool(min_size=2, max_size=args.workers + 4)
    print(f"Postgres  : {get_pg_url().split('@')[-1]}", file=sys.stderr)
    print(f"Court     : {args.court}  workers={args.workers}  rate={args.rate}/min  "
          f"force={args.force}", file=sys.stderr)

    rows = fetch_rows(pool, args.court, args.limit, args.order)
    print(f"Loaded {len(rows)} decisions to process", file=sys.stderr)
    if not rows:
        print("Nothing to do.", file=sys.stderr)
        return

    if args.provider == "openrouter":
        client = OpenRouterClient(
            model=args.model or "google/gemini-2.0-flash-001",
            fallback_model="google/gemini-2.0-flash-001",
            rate_limit_per_minute=args.rate,
        )
    else:
        client = SyntheticClient(model=args.model, rate_limit_per_minute=args.rate)
    print(f"Provider: {args.provider}  model={client.model}", file=sys.stderr)

    agg = run_many(
        rows, pool=pool, client=client,
        n_workers=args.workers,
        progress_every=max(10, len(rows) // 40),
        force=args.force,
    )
    print("\n════ FINAL ════")
    print(json.dumps(agg, indent=2, default=str))


if __name__ == "__main__":
    main()
