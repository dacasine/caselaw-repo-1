"""Tiny interactive-style retrieval test on the PA-RAG chunks DB.

Takes a query on the command line, encodes it with BGE-M3, runs a
sqlite-vec k-NN lookup, and prints the top-K chunks with their summary
headers + a text snippet, so we can eyeball whether the Phase 3 + Phase 4
pipeline actually surfaces relevant content.

Usage:
    .venv/bin/python scripts/parag/search.py "interruption de prescription en poursuite"
    .venv/bin/python scripts/parag/search.py --k 20 "concubinage qualifié" --language fr
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import struct
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=FutureWarning)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import sqlite_vec
from search_stack.parag.embedder import (
    MAX_SEQ_LENGTH,
    build_embedding_input,
    load_embedder,
)
from db_schema_parag import DEFAULT_PARAG_DB


def _vec_blob(vec) -> bytes:
    if hasattr(vec, "tolist"):
        vec = vec.tolist()
    return struct.pack(f"{len(vec)}f", *vec)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", help="Query text in FR / DE / IT")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--language", default=None, help="Filter: de, fr, it")
    ap.add_argument("--court", default=None, help="Filter: bge, bger, ge_gerichte, ...")
    ap.add_argument("--snippet", type=int, default=240, help="Chars of body to show")
    ap.add_argument("--parag-db", type=Path, default=DEFAULT_PARAG_DB)
    args = ap.parse_args()

    print(f"Loading BGE-M3 (CPU)…", file=sys.stderr)
    model = load_embedder(device="cpu")
    q_vec = model.encode(
        [args.query],
        convert_to_numpy=True,
        normalize_embeddings=True,
        show_progress_bar=False,
    )[0]

    conn = sqlite3.connect(str(args.parag_db))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    filters = ""
    params: list = []
    if args.language:
        filters += " AND c.language = ?"
        params.append(args.language)
    if args.court:
        filters += " AND c.court = ?"
        params.append(args.court)

    sql = f"""
        SELECT v.rowid,
               v.distance,
               c.decision_id,
               c.court,
               c.language,
               c.considerant_number,
               c.summary,
               c.cleaned
        FROM vec_chunks v
        JOIN chunks c ON c.id = v.rowid
        WHERE v.embedding MATCH ? AND k = ?{filters}
        ORDER BY v.distance
    """
    params = [_vec_blob(q_vec), args.k] + params
    rows = conn.execute(sql, params).fetchall()

    print(f"\n═══ QUERY ═══")
    print(f"  {args.query!r}")
    print(f"  filters: language={args.language} court={args.court} k={args.k}")
    print()
    for i, r in enumerate(rows, 1):
        chunk_id, distance, did, court, lang, num, summary, body = r
        # distance is squared L2; for normalised vectors cos = 1 - L2^2 / 2
        cos = 1 - distance / 2
        print(f"── {i:>2} ─ cos={cos:.3f} ─ {did} [{lang}] {court} ── cons {num}")
        if summary:
            print(f"    ➤ {summary}")
        body_s = " ".join(body.split())[: args.snippet]
        print(f"    {body_s}…")
        print()


if __name__ == "__main__":
    main()
