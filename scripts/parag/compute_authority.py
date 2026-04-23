"""One-shot post-processing to populate decision_authority.

Runs, in order:
    1. authority.populate_authority           (court_level, atf, static score)
    2. validity_propagation.propagate         (validity_status from Phase 5)
    3. pagerank.populate_pagerank             (time-decayed PageRank)

Reads CASELAW_PG_URL from environment. Idempotent.

Usage:
    .venv/bin/python scripts/parag/compute_authority.py [--skip-pagerank]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from time import monotonic

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from search_stack.parag import authority, pagerank, validity_propagation
from search_stack.parag.pg_conn import get_conn, get_pg_url


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-pagerank", action="store_true")
    ap.add_argument("--skip-authority", action="store_true")
    ap.add_argument("--skip-validity", action="store_true")
    args = ap.parse_args()

    print(f"Postgres : {get_pg_url().split('@')[-1]}", flush=True)
    conn = get_conn()
    report: dict[str, object] = {}

    if not args.skip_authority:
        t0 = monotonic()
        print("[1/3] computing court_level + atf_published + static authority_score…",
              flush=True)
        report["authority"] = authority.populate_authority(conn)
        report["authority"]["elapsed_s"] = round(monotonic() - t0, 1)

    if not args.skip_validity:
        t0 = monotonic()
        print("[2/3] propagating validity_status from Phase 5 prior_case_treatment…",
              flush=True)
        report["validity"] = validity_propagation.propagate(conn)
        report["validity"]["elapsed_s"] = round(monotonic() - t0, 1)

    if not args.skip_pagerank:
        t0 = monotonic()
        print("[3/3] computing time-decayed PageRank…", flush=True)
        report["pagerank"] = pagerank.populate_pagerank(conn)
        report["pagerank"]["elapsed_s"] = round(monotonic() - t0, 1)

    conn.close()
    print("\nREPORT:")
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
