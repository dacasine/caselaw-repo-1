"""Persistence helpers — writing chunks and state to the PA-RAG DB."""

from __future__ import annotations

import hashlib
import sqlite3

from search_stack.parag.sac_builder import BuildStats, Chunk


def chunk_hash(cleaned: str) -> str:
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def source_hash(full_text: str) -> str:
    return hashlib.sha256(full_text.encode("utf-8")).hexdigest()


def upsert_chunks(
    conn: sqlite3.Connection,
    chunks: list[Chunk],
    prompt_version: int,
) -> None:
    """Replace all chunks for the decisions present in `chunks`. Uses a
    delete-then-insert pattern so a re-run is safe (chunks table has a
    UNIQUE constraint on (decision_id, considerant_number, span_start))."""
    if not chunks:
        return
    decision_ids = {c.decision_id for c in chunks}
    cur = conn.cursor()
    # Clear previous chunks for these decisions.
    cur.executemany(
        "DELETE FROM chunks WHERE decision_id = ?",
        [(d,) for d in decision_ids],
    )
    cur.executemany(
        """
        INSERT INTO chunks (
            decision_id, court, language, considerant_number, depth,
            span_start, span_end, raw_length, cleaned, summary,
            summary_source, chunk_hash, prompt_version
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                c.decision_id,
                c.court,
                c.language,
                c.considerant_number,
                c.depth,
                c.span_start,
                c.span_end,
                c.raw_length,
                c.cleaned,
                c.summary,
                c.summary_source,
                chunk_hash(c.cleaned),
                prompt_version,
            )
            for c in chunks
        ],
    )


def upsert_state(
    conn: sqlite3.Connection,
    *,
    decision_id: str,
    court: str,
    parser_name: str,
    stats: BuildStats,
    src_hash: str,
    prompt_version: int,
    status: str,
    error_message: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO enrichment_state (
            decision_id, court, parser_name, fallback_used,
            n_chunks, n_stubs, n_self_suff, n_summarized, n_errors,
            llm_calls, llm_latency_s, prompt_tokens, completion_tokens,
            source_hash, prompt_version, status, error_message, processed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(decision_id) DO UPDATE SET
            court            = excluded.court,
            parser_name      = excluded.parser_name,
            fallback_used    = excluded.fallback_used,
            n_chunks         = excluded.n_chunks,
            n_stubs          = excluded.n_stubs,
            n_self_suff      = excluded.n_self_suff,
            n_summarized     = excluded.n_summarized,
            n_errors         = excluded.n_errors,
            llm_calls        = excluded.llm_calls,
            llm_latency_s    = excluded.llm_latency_s,
            prompt_tokens    = excluded.prompt_tokens,
            completion_tokens= excluded.completion_tokens,
            source_hash      = excluded.source_hash,
            prompt_version   = excluded.prompt_version,
            status           = excluded.status,
            error_message    = excluded.error_message,
            processed_at     = datetime('now')
        """,
        (
            decision_id,
            court,
            parser_name,
            stats.fallback_used,
            stats.chunks_total,
            stats.stubs,
            stats.self_sufficient,
            stats.summarized,
            stats.llm_errors,
            stats.llm_calls,
            stats.llm_latency_s,
            stats.prompt_tokens,
            stats.completion_tokens,
            src_hash,
            prompt_version,
            status,
            error_message,
        ),
    )


def should_skip(
    conn: sqlite3.Connection,
    decision_id: str,
    src_hash: str,
    prompt_version: int,
) -> bool:
    """Return True if this decision was already processed successfully
    with the same source hash and prompt version."""
    row = conn.execute(
        "SELECT source_hash, prompt_version, status FROM enrichment_state "
        "WHERE decision_id = ?",
        (decision_id,),
    ).fetchone()
    if row is None:
        return False
    stored_hash, stored_version, status = row
    return (
        status == "ok"
        and stored_hash == src_hash
        and stored_version >= prompt_version
    )
