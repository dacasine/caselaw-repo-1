"""Orchestrator: one-decision processing pipeline + parallel runner.

process_decision(row, ...) handles the full flow for a single decision:
    parse → build chunks → generate summaries → persist chunks + state.

run_many(rows, ...) drives a thread pool for parallel execution over a
list of decision rows. The LLM client's own rate limiter ensures we stay
under the synthetic.new quota regardless of worker count.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from db_schema_parag import PROMPT_VERSION
from search_stack.parag.llm_client import SyntheticClient
from search_stack.parag.parsers import parse
from search_stack.parag.persistence import (
    should_skip,
    source_hash,
    upsert_chunks,
    upsert_state,
)
from search_stack.parag.sac_builder import (
    BuildStats,
    build_chunks_from_parsed,
    generate_summaries,
)


@dataclass
class DecisionRow:
    decision_id: str
    court: str
    language: str
    full_text: str
    regeste: str


@dataclass
class ProcessResult:
    decision_id: str
    status: str                  # 'ok' | 'skipped' | 'error' | 'empty'
    stats: BuildStats | None = None
    error: str | None = None
    latency_s: float = 0.0


def process_decision(
    row: DecisionRow,
    *,
    parag_conn: sqlite3.Connection,
    parag_lock: threading.Lock,
    client: SyntheticClient,
    force: bool = False,
) -> ProcessResult:
    """Process one decision end-to-end. Thread-safe via `parag_lock`
    around DB writes (SQLite is single-writer)."""
    t0 = time.monotonic()
    src_hash = source_hash(row.full_text or "")

    if not force:
        with parag_lock:
            if should_skip(parag_conn, row.decision_id, src_hash, PROMPT_VERSION):
                return ProcessResult(row.decision_id, "skipped",
                                     latency_s=time.monotonic() - t0)

    try:
        parsed = parse(row.decision_id, row.court, row.language, row.full_text or "")
    except Exception as exc:
        return ProcessResult(row.decision_id, "error", error=f"parse: {exc}",
                             latency_s=time.monotonic() - t0)

    chunks = build_chunks_from_parsed(parsed, row.court, row.full_text or "")
    stats = BuildStats()
    if not chunks:
        # Empty: record state so we don't revisit.
        with parag_lock:
            upsert_state(
                parag_conn,
                decision_id=row.decision_id,
                court=row.court,
                parser_name=parsed.parser_name,
                stats=stats,
                src_hash=src_hash,
                prompt_version=PROMPT_VERSION,
                status="empty",
            )
            parag_conn.commit()
        return ProcessResult(row.decision_id, "empty", stats=stats,
                             latency_s=time.monotonic() - t0)

    try:
        stats = generate_summaries(chunks, client, regeste=row.regeste or "")
    except Exception as exc:
        # Persist chunks we have anyway (without summaries) so parser
        # output isn't lost on LLM outage.
        with parag_lock:
            upsert_chunks(parag_conn, chunks, PROMPT_VERSION)
            upsert_state(
                parag_conn,
                decision_id=row.decision_id,
                court=row.court,
                parser_name=parsed.parser_name,
                stats=stats,
                src_hash=src_hash,
                prompt_version=PROMPT_VERSION,
                status="error",
                error_message=f"summaries: {exc}",
            )
            parag_conn.commit()
        return ProcessResult(row.decision_id, "error", stats=stats,
                             error=f"summaries: {exc}",
                             latency_s=time.monotonic() - t0)

    with parag_lock:
        upsert_chunks(parag_conn, chunks, PROMPT_VERSION)
        upsert_state(
            parag_conn,
            decision_id=row.decision_id,
            court=row.court,
            parser_name=parsed.parser_name,
            stats=stats,
            src_hash=src_hash,
            prompt_version=PROMPT_VERSION,
            status="ok",
        )
        parag_conn.commit()

    return ProcessResult(row.decision_id, "ok", stats=stats,
                         latency_s=time.monotonic() - t0)


def run_many(
    rows: list[DecisionRow],
    *,
    parag_conn: sqlite3.Connection,
    client: SyntheticClient,
    n_workers: int = 4,
    progress_every: int = 25,
    force: bool = False,
) -> dict:
    """Thread-pool orchestrator. Returns aggregate statistics dict."""
    parag_lock = threading.Lock()
    agg = {
        "total": len(rows),
        "ok": 0,
        "skipped": 0,
        "error": 0,
        "empty": 0,
        "chunks_summarized": 0,
        "llm_calls": 0,
        "llm_latency_s": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }
    t_start = time.monotonic()

    def work(row: DecisionRow) -> ProcessResult:
        return process_decision(
            row,
            parag_conn=parag_conn,
            parag_lock=parag_lock,
            client=client,
            force=force,
        )

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [pool.submit(work, r) for r in rows]
        done = 0
        for fut in as_completed(futures):
            r = fut.result()
            done += 1
            agg[r.status] = agg.get(r.status, 0) + 1
            if r.stats is not None:
                agg["chunks_summarized"] += r.stats.summarized
                agg["llm_calls"] += r.stats.llm_calls
                agg["llm_latency_s"] += r.stats.llm_latency_s
                agg["prompt_tokens"] += r.stats.prompt_tokens
                agg["completion_tokens"] += r.stats.completion_tokens

            if done % progress_every == 0 or done == len(rows):
                elapsed = time.monotonic() - t_start
                rate = done / elapsed if elapsed > 0 else 0
                eta = (len(rows) - done) / rate if rate > 0 else 0
                quota_str = client.quota.describe() if client.quota else ""
                print(
                    f"  [{done}/{len(rows)}] "
                    f"ok={agg['ok']} skip={agg['skipped']} "
                    f"err={agg['error']} empty={agg['empty']} "
                    f"| llm={agg['llm_calls']} "
                    f"summ={agg['chunks_summarized']} "
                    f"| rate={rate*60:.1f}/min eta={eta/60:.1f}min "
                    f"| {quota_str}",
                    file=sys.stderr,
                    flush=True,
                )

    agg["wall_time_s"] = time.monotonic() - t_start
    return agg
