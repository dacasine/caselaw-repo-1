"""Smoke test for the batched SAC builder.

Samples 5 random ATF decisions, parses them, materialises chunks, applies
the triage (skip stubs / self-sufficient), requests summary headers in
batches of SAC_BATCH_SIZE, then prints before/after plus aggregate stats.

Usage:
    PYTHONPATH=. python3 scripts/parag/poc_sac_batch.py
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from search_stack.parag.llm_client import SyntheticClient
from search_stack.parag.parsers import parse
from search_stack.parag.sac_builder import (
    BuildStats,
    build_chunks_from_parsed,
    generate_summaries,
)


DB_PATH = Path.home() / ".swiss-caselaw" / "decisions.db"


def main() -> None:
    client = SyntheticClient(rate_limit_per_minute=30)
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT decision_id, court, language, full_text, regeste FROM decisions "
        "WHERE court='bge' AND length(full_text)>12000 ORDER BY RANDOM() LIMIT 5"
    ).fetchall()

    overall = BuildStats()
    t0 = time.monotonic()

    for did, court, lang, txt, regeste in rows:
        parsed = parse(did, court, lang, txt)
        chunks = build_chunks_from_parsed(parsed, court, txt)
        if not chunks:
            print(f"\n[{did}] no chunks produced, skipping")
            continue

        print(f"\n══════════ {did} [{lang}] ══════════")
        print(f"  parser={parsed.parser_name}  considérants={len(parsed.considerants)}  "
              f"chunks={len(chunks)}  "
              f"stubs={sum(1 for c in chunks if c.summary_source == 'stub')}  "
              f"self_suff={sum(1 for c in chunks if c.summary_source == 'self_sufficient')}  "
              f"to_llm={sum(1 for c in chunks if c.reason_skipped is None)}")

        stats = generate_summaries(chunks, client, regeste=regeste or "")
        # Aggregate
        overall.chunks_total += stats.chunks_total
        overall.stubs += stats.stubs
        overall.self_sufficient += stats.self_sufficient
        overall.summarized += stats.summarized
        overall.llm_calls += stats.llm_calls
        overall.llm_errors += stats.llm_errors
        overall.llm_latency_s += stats.llm_latency_s
        overall.prompt_tokens += stats.prompt_tokens
        overall.completion_tokens += stats.completion_tokens
        overall.fallback_used += stats.fallback_used

        # Print first 3 chunks of each decision to spot-check
        for ch in chunks[:3]:
            print(f"\n  ── {ch.considerant_number} (depth={ch.depth}) len={len(ch.cleaned)} "
                  f"source={ch.summary_source} ──")
            print(f"    text  : {ch.cleaned[:160]!r}")
            if ch.summary:
                print(f"    header: {ch.summary}")

    dt = time.monotonic() - t0
    print(f"\n════════════════ AGGREGATE ════════════════")
    print(f"  decisions processed : {len(rows)}")
    print(f"  chunks total        : {overall.chunks_total}")
    print(f"    stubs skipped     : {overall.stubs}")
    print(f"    self-sufficient   : {overall.self_sufficient}")
    print(f"    summarised        : {overall.summarized}")
    print(f"    errors            : {overall.llm_errors}")
    print(f"  LLM calls           : {overall.llm_calls}")
    print(f"  Kimi fallback used  : {overall.fallback_used} time(s)")
    print(f"  tokens in           : {overall.prompt_tokens}")
    print(f"  tokens out          : {overall.completion_tokens}")
    print(f"  LLM latency (sum)   : {overall.llm_latency_s:.1f} s")
    print(f"  wall time           : {dt:.1f} s")
    if overall.llm_calls:
        print(f"  avg latency/call    : {overall.llm_latency_s / overall.llm_calls:.1f} s")
        print(f"  avg chunks/call     : {overall.summarized / max(overall.llm_calls,1):.1f}")


if __name__ == "__main__":
    main()
