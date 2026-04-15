"""Batch encode chunks with BGE-M3 and store vectors in parag_chunks.db.

Usage:
    .venv/bin/python scripts/parag/run_embed.py [--limit N] [--batch 16]
                                                [--where "court='bge'"]

Incremental and idempotent: skips chunks already encoded at the current
EMBEDDING_VERSION. Bumping EMBEDDING_VERSION triggers re-encoding.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from db_schema_parag import DEFAULT_PARAG_DB, EMBEDDING_MODEL, init_parag_schema
from search_stack.parag.embedder import (
    encode_and_store,
    load_embedder,
    pick_device,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parag-db", type=Path, default=DEFAULT_PARAG_DB)
    ap.add_argument("--limit", type=int, default=None,
                    help="Max chunks to encode this run")
    ap.add_argument("--batch", type=int, default=16,
                    help="Encoder batch size (default 16)")
    ap.add_argument("--where", default="",
                    help="Extra SQL WHERE on chunks, e.g. \"court='bge'\"")
    ap.add_argument("--device", default=None,
                    help="Force device: mps, cuda, cpu (default: auto)")
    args = ap.parse_args()

    device = args.device or pick_device()
    print(f"Parag DB : {args.parag_db}")
    print(f"Model    : {EMBEDDING_MODEL}")
    print(f"Device   : {device}")
    print(f"Batch    : {args.batch}")

    conn = init_parag_schema(args.parag_db)
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
