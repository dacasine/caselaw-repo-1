"""Full SAC run over a court (default: bge = ATF).

Usage:
    PYTHONPATH=. python3 scripts/parag/run_sac.py [--court bge] [--limit N]
                                                  [--workers 4] [--rate 30]
                                                  [--force]

Idempotent. Re-running without --force skips any decision whose
(full_text hash, prompt_version) was already processed successfully.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from db_schema_parag import DEFAULT_PARAG_DB, init_parag_schema
from search_stack.parag.llm_client import SyntheticClient
from search_stack.parag.worker import DecisionRow, run_many


DEFAULT_SOURCE_DB = Path.home() / ".swiss-caselaw" / "decisions.db"


def fetch_rows(
    src_db: Path,
    court: str,
    limit: int | None,
    order: str = "id",
    where_extra: str = "",
) -> list[DecisionRow]:
    conn = sqlite3.connect(str(src_db))
    try:
        q_where = "court = ? AND full_text IS NOT NULL AND length(full_text) > 500"
        if where_extra:
            q_where += f" AND ({where_extra})"
        order_sql = {
            "id": "ORDER BY decision_id",
            "random": "ORDER BY RANDOM()",
            "length": "ORDER BY length(full_text) DESC",
        }.get(order, "ORDER BY decision_id")
        q = (
            f"SELECT decision_id, court, language, full_text, COALESCE(regeste,'') "
            f"FROM decisions WHERE {q_where} {order_sql}"
        )
        args: tuple = (court,)
        if limit:
            q += " LIMIT ?"
            args = (court, limit)
        rows = [
            DecisionRow(
                decision_id=r[0],
                court=r[1],
                language=r[2],
                full_text=r[3],
                regeste=r[4],
            )
            for r in conn.execute(q, args).fetchall()
        ]
        return rows
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--court", default="bge", help="Court code to process (default: bge)")
    ap.add_argument("--limit", type=int, default=None, help="Limit for testing")
    ap.add_argument("--workers", type=int, default=4, help="Parallel workers (default 4)")
    ap.add_argument("--rate", type=int, default=30, help="LLM requests per minute (default 30)")
    ap.add_argument("--force", action="store_true", help="Reprocess even if already up-to-date")
    ap.add_argument("--order", choices=["id", "random", "length"], default="id",
                    help="Row ordering (default: id = deterministic)")
    ap.add_argument("--where", default="", help="Extra SQL WHERE clause")
    ap.add_argument("--src-db", type=Path, default=DEFAULT_SOURCE_DB)
    ap.add_argument("--parag-db", type=Path, default=DEFAULT_PARAG_DB)
    args = ap.parse_args()

    print(f"Source DB  : {args.src_db}", file=sys.stderr)
    print(f"Parag DB   : {args.parag_db}", file=sys.stderr)
    print(f"Court      : {args.court}  workers={args.workers}  rate={args.rate}/min  "
          f"force={args.force}", file=sys.stderr)

    parag_conn = init_parag_schema(args.parag_db)
    rows = fetch_rows(args.src_db, args.court, args.limit, args.order, args.where)
    print(f"Loaded {len(rows)} decisions to process", file=sys.stderr)
    if not rows:
        print("Nothing to do.", file=sys.stderr)
        return

    client = SyntheticClient(rate_limit_per_minute=args.rate)

    agg = run_many(
        rows,
        parag_conn=parag_conn,
        client=client,
        n_workers=args.workers,
        progress_every=max(10, len(rows) // 40),
        force=args.force,
    )
    parag_conn.close()

    print("\n════ FINAL ════")
    print(json.dumps(agg, indent=2, default=str))


if __name__ == "__main__":
    main()
