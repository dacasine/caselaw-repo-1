"""Persistence helpers — writing chunks and state to the PA-RAG DB."""

from __future__ import annotations

import hashlib

import psycopg

from search_stack.parag.sac_builder import BuildStats, Chunk


def chunk_hash(cleaned: str) -> str:
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def source_hash(full_text: str) -> str:
    return hashlib.sha256(full_text.encode("utf-8")).hexdigest()


def upsert_chunks(
    conn: psycopg.Connection,
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
        "DELETE FROM chunks WHERE decision_id = %s",
        [(d,) for d in decision_ids],
    )
    cur.executemany(
        """
        INSERT INTO chunks (
            decision_id, court, language, considerant_number, depth,
            span_start, span_end, raw_length, cleaned, summary,
            summary_source, chunk_hash, prompt_version
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
    conn: psycopg.Connection,
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
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT(decision_id) DO UPDATE SET
            court            = EXCLUDED.court,
            parser_name      = EXCLUDED.parser_name,
            fallback_used    = EXCLUDED.fallback_used,
            n_chunks         = EXCLUDED.n_chunks,
            n_stubs          = EXCLUDED.n_stubs,
            n_self_suff      = EXCLUDED.n_self_suff,
            n_summarized     = EXCLUDED.n_summarized,
            n_errors         = EXCLUDED.n_errors,
            llm_calls        = EXCLUDED.llm_calls,
            llm_latency_s    = EXCLUDED.llm_latency_s,
            prompt_tokens    = EXCLUDED.prompt_tokens,
            completion_tokens= EXCLUDED.completion_tokens,
            source_hash      = EXCLUDED.source_hash,
            prompt_version   = EXCLUDED.prompt_version,
            status           = EXCLUDED.status,
            error_message    = EXCLUDED.error_message,
            processed_at     = now()
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
    conn: psycopg.Connection,
    decision_id: str,
    src_hash: str,
    prompt_version: int,
) -> bool:
    """Return True if this decision was already processed successfully
    with the same source hash and prompt version."""
    row = conn.execute(
        "SELECT source_hash, prompt_version, status FROM enrichment_state "
        "WHERE decision_id = %s",
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
