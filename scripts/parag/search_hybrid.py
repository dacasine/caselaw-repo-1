"""Hybrid PA-RAG retrieval test CLI — Phase 7.

Combines BM25 (FTS5) + ANN (sqlite-vec) + authority rerank. Shows per-
result scoring breakdown so you can see why each result ranks where it
does.

Prerequisites:
  - chunks_fts exists (run scripts/parag/init_fts5.py first)
  - decision_authority populated (run scripts/parag/compute_authority.py)
  - BGE-M3 embedder available (venv already has it)

Usage:
    .venv/bin/python scripts/parag/search_hybrid.py "interruption prescription LP" \\
        --k 10 --language fr
"""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=FutureWarning)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from search_stack.parag.embedder import load_embedder
from search_stack.parag.retrieval import (
    RetrievalFilters,
    open_retrieval_db,
    retrieve,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", help="Query text in FR / DE / IT")
    ap.add_argument("--k", type=int, default=10, help="top-N final results")
    ap.add_argument("--k-bm25", type=int, default=100)
    ap.add_argument("--k-ann",  type=int, default=100)
    ap.add_argument("--language", default=None)
    ap.add_argument("--court", nargs="+", default=None)
    ap.add_argument("--court-level-min", type=int, default=None)
    ap.add_argument("--include-overruled", action="store_true",
                    help="Do NOT filter out overruled decisions")
    ap.add_argument("--date-from", default=None, help="ISO date")
    ap.add_argument("--date-to", default=None)
    ap.add_argument("--explain", action="store_true",
                    help="Show per-component scoring breakdown")
    args = ap.parse_args()

    filters = RetrievalFilters(
        language=args.language,
        courts=tuple(args.court) if args.court else None,
        court_level_min=args.court_level_min,
        exclude_overruled=not args.include_overruled,
        date_from=args.date_from,
        date_to=args.date_to,
    )

    print("loading BGE-M3 (CPU)…", file=sys.stderr)
    model = load_embedder(device="cpu")
    qvec = model.encode([args.query], convert_to_numpy=True,
                        normalize_embeddings=True, show_progress_bar=False)[0]

    conn = open_retrieval_db()
    results = retrieve(
        conn,
        query=args.query, query_vec=qvec,
        filters=filters,
        k_bm25=args.k_bm25, k_ann=args.k_ann, top_n=args.k,
    )

    print(f"\n═══ QUERY ═══  {args.query!r}")
    print(f"filters: {filters}")
    print(f"→ {len(results)} results\n")

    for i, r in enumerate(results, 1):
        badges = []
        if r.atf_published: badges.append("ATF")
        badges.append(f"L{r.court_level}")
        if r.validity_status != "valid":
            badges.append(r.validity_status.upper())
        badge_str = " ".join(f"[{b}]" for b in badges)

        print(f"── {i:>2}  {r.decision_id}  {badge_str}  "
              f"[{r.language}] cons {r.considerant_number}")
        print(f"      final={r.final_score:.4f}  rrf={r.rrf_score:.3f}  "
              f"cos={r.cosine:.3f if r.cosine else 0:.3f}  "
              f"pr={r.pagerank_temporal:.6f}")
        if r.summary:
            print(f"      ➤ {r.summary}")
        print(f"      {' '.join((r.cleaned_snippet or '').split())[:260]}…")
        if args.explain:
            bd = r.breakdown
            print(f"      breakdown: text={bd.get('text',0):.3f} "
                  f"pr={bd.get('pagerank',0):.3f} "
                  f"court={bd.get('court',0):.3f} "
                  f"val×{bd.get('validity_mul',1):.2f}")
        print()


if __name__ == "__main__":
    main()
