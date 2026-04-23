"""Batch encode chunks with BGE-M3 and store vectors in Postgres+pgvector.

Usage:
    .venv/bin/python scripts/parag/run_embed.py [--limit N] [--batch 16]
                                                [--where "court='bge'"]

Reads CASELAW_PG_URL from environment. Incremental and idempotent.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from db_schema_parag import EMBEDDING_MODEL
from search_stack.parag.embedder import encode_and_store, load_embedder, pick_device
from search_stack.parag.pg_conn import get_conn, get_pg_url


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--where", default="")
    ap.add_argument("--device", default=None)
    args = ap.parse_args()

    device = args.device or pick_device()
    print(f"Postgres : {get_pg_url().split('@')[-1]}")
    print(f"Model    : {EMBEDDING_MODEL}")
    print(f"Device   : {device}")
    print(f"Batch    : {args.batch}")

    conn = get_conn()
    model = load_embedder(device=device)

    stats = encode_and_store(
        conn, model,
        batch_size=args.batch,
        limit=args.limit,
        where_extra=args.where,
    )
    conn.close()

    print("\n════ FINAL ════")
    print(json.dumps(stats, indent=2, default=str))


if __name__ == "__main__":
    main()
