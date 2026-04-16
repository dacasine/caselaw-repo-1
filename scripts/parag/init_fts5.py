"""One-shot setup for the chunks_fts FTS5 index (required for BM25 in Phase 7).

Reads the chunks table and populates chunks_fts. Idempotent — re-running
is a no-op thanks to triggers that keep chunks_fts in sync from now on.

Usage:
    .venv/bin/python scripts/parag/init_fts5.py
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from db_schema_parag import DEFAULT_PARAG_DB
from search_stack.parag.retrieval import ensure_fts5_index


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parag-db", type=Path, default=DEFAULT_PARAG_DB)
    args = ap.parse_args()

    conn = sqlite3.connect(str(args.parag_db), timeout=60.0)
    conn.executescript("PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL;")

    ensure_fts5_index(conn)
    conn.commit()

    # Check if already populated
    n_fts = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    if n_fts >= n_chunks:
        print(f"chunks_fts already populated: {n_fts} rows (matches chunks table)")
        conn.close()
        return

    print(f"populating chunks_fts from chunks ({n_chunks} rows)…")
    t0 = time.monotonic()
    conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
    conn.commit()
    dt = time.monotonic() - t0
    n_fts_after = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    print(f"done in {dt:.1f}s — chunks_fts now has {n_fts_after} rows")
    conn.close()


if __name__ == "__main__":
    main()
