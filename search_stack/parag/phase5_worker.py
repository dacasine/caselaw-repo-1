"""Phase 5 enrichment worker — per-decision LLM extraction + persistence.

Flow per decision:
    1. build DecisionContext (full_text + SAC chunk headers + dispositif)
    2. call LLM with SYSTEM_PROMPT_FULL (via OpenRouterClient)
    3. parse JSON, validate schema, canonicalise BGE references
    4. upsert decision_enrichment row
    5. for each chunk of this decision:
         run CitationResolver (regex-only on chunk text — LLM legal_basis
         is stored as JSON in decision_enrichment, not duplicated per chunk)
         upsert chunk_law_citations / chunk_case_citations
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

from db_schema_parag import PROMPT_VERSION
from search_stack.parag.citation_resolver import CitationResolver
from search_stack.parag.enrichment import (
    ENRICHMENT_PROMPT_VERSION,
    SYSTEM_PROMPT_FULL,
    SYSTEM_PROMPT_LIGHT,
    DecisionContext,
    build_user_prompt,
    canonicalise_bge,
    validate_full,
)
from search_stack.parag.openrouter_client import OpenRouterClient


@dataclass
class Phase5Row:
    decision_id: str
    court: str
    language: str
    date_iso: str
    chamber: str | None
    regeste: str
    full_text: str


@dataclass
class Phase5Result:
    decision_id: str
    status: str                    # 'ok' | 'skipped' | 'error' | 'json_fail' | 'schema_invalid'
    latency_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    n_principle_questions: int = 0
    n_prior_treatment: int = 0
    n_law_citations: int = 0
    n_case_citations: int = 0
    error: str | None = None


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def _load_chunks_for_decision(
    conn: psycopg.Connection, decision_id: str
) -> tuple[list[str], list[tuple[int, str]], str | None]:
    """Return (chunk_headers, [(chunk_id, cleaned), ...], dispositif_text).

    `dispositif_text` = cleaned of the last chunk heuristic (same as the
    validator uses).
    """
    rows = conn.execute(
        "SELECT id, cleaned, summary FROM chunks "
        "WHERE decision_id=%s ORDER BY span_start",
        (decision_id,),
    ).fetchall()
    chunk_id_texts = [(r[0], r[1]) for r in rows]
    headers = [r[2] for r in rows if r[2]]
    dispositif = rows[-1][1] if rows else None
    return headers, chunk_id_texts, dispositif


def _source_hash(full_text: str) -> str:
    return hashlib.sha256(full_text.encode("utf-8")).hexdigest()


def _should_skip(
    conn: psycopg.Connection,
    decision_id: str,
    src_hash: str,
    prompt_version: int,
) -> bool:
    row = conn.execute(
        "SELECT source_hash, prompt_version, status FROM decision_enrichment "
        "WHERE decision_id=%s",
        (decision_id,),
    ).fetchone()
    if row is None:
        return False
    # Skip ok/schema_invalid (done) AND 403 errors (content blocked by Gemini — needs different model)
    if row[2] in ("ok", "schema_invalid"):
        return row[0] == src_hash and row[1] >= prompt_version
    if row[2] == "error":
        # Check if it's a 403 content filter — don't retry with same model
        error_msg = conn.execute(
            "SELECT error_message FROM decision_enrichment WHERE decision_id=%s",
            (decision_id,),
        ).fetchone()
        if error_msg and error_msg[0] and "403" in error_msg[0]:
            return True
    return False


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _store_decision_enrichment(
    conn: psycopg.Connection,
    *,
    row: Phase5Row,
    obj: dict,
    src_hash: str,
    latency_s: float,
    prompt_tokens: int,
    completion_tokens: int,
    status: str,
    error_message: str | None = None,
) -> None:
    sort = obj.get("sort", {})
    conn.execute(
        """
        INSERT INTO decision_enrichment (
            decision_id, court, procedural_stage, outcome, subject_matter,
            principle_questions, obiter_dicta, doctrine_discussion,
            language_detected, source_hash, prompt_version,
            llm_latency_s, prompt_tokens, completion_tokens,
            status, error_message, processed_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT(decision_id) DO UPDATE SET
            court               = EXCLUDED.court,
            procedural_stage    = EXCLUDED.procedural_stage,
            outcome             = EXCLUDED.outcome,
            subject_matter      = EXCLUDED.subject_matter,
            principle_questions = EXCLUDED.principle_questions,
            obiter_dicta        = EXCLUDED.obiter_dicta,
            doctrine_discussion = EXCLUDED.doctrine_discussion,
            language_detected   = EXCLUDED.language_detected,
            source_hash         = EXCLUDED.source_hash,
            prompt_version      = EXCLUDED.prompt_version,
            llm_latency_s       = EXCLUDED.llm_latency_s,
            prompt_tokens       = EXCLUDED.prompt_tokens,
            completion_tokens   = EXCLUDED.completion_tokens,
            status              = EXCLUDED.status,
            error_message       = EXCLUDED.error_message,
            processed_at        = now()
        """,
        (
            row.decision_id,
            row.court,
            sort.get("procedural_stage"),
            sort.get("outcome"),
            sort.get("subject_matter"),
            json.dumps(obj.get("principle_questions") or [], ensure_ascii=False),
            json.dumps(obj.get("obiter_dicta") or [], ensure_ascii=False),
            json.dumps(obj.get("doctrine_discussion") or {}, ensure_ascii=False),
            row.language,
            src_hash,
            ENRICHMENT_PROMPT_VERSION,
            latency_s,
            prompt_tokens,
            completion_tokens,
            status,
            error_message,
        ),
    )


# ---------------------------------------------------------------------------
# Per-decision orchestrator
# ---------------------------------------------------------------------------

def process_decision(
    row: Phase5Row,
    *,
    pool: ConnectionPool,
    client: OpenRouterClient,
    resolver: CitationResolver,
    use_light_prompt: bool = False,
    force: bool = False,
) -> Phase5Result:
    t_start = time.monotonic()
    src_hash = _source_hash(row.full_text or "")

    if not force:
        with pool.connection() as conn:
            if _should_skip(conn, row.decision_id, src_hash, ENRICHMENT_PROMPT_VERSION):
                return Phase5Result(row.decision_id, "skipped",
                                    latency_s=time.monotonic() - t_start)

    # Build context from SAC chunks (idempotent — pure reads)
    with pool.connection() as conn:
        headers, chunk_id_texts, dispositif = _load_chunks_for_decision(
            conn, row.decision_id
        )

    ctx = DecisionContext(
        decision_id=row.decision_id,
        court=row.court,
        language=row.language,
        date_iso=row.date_iso or "",
        chamber=row.chamber,
        regeste=row.regeste,
        full_text=row.full_text or "",
        chunk_headers=headers,
        dispositif_text=dispositif,
    )
    system = SYSTEM_PROMPT_LIGHT if use_light_prompt else SYSTEM_PROMPT_FULL
    user = build_user_prompt(ctx, light=use_light_prompt)

    # LLM call (no DB connection held)
    try:
        resp = client.chat(system=system, user=user, max_tokens=12000, temperature=0.1)
    except Exception as exc:
        with pool.connection() as conn:
            _store_decision_enrichment(
                conn, row=row, obj={},
                src_hash=src_hash, latency_s=time.monotonic() - t_start,
                prompt_tokens=0, completion_tokens=0,
                status="error", error_message=f"llm: {exc}"[:500],
            )
            conn.commit()
        return Phase5Result(row.decision_id, "error",
                            latency_s=time.monotonic() - t_start,
                            error=f"llm: {exc}"[:200])

    raw = resp.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]

    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        with pool.connection() as conn:
            _store_decision_enrichment(
                conn, row=row, obj={},
                src_hash=src_hash, latency_s=resp.latency_s,
                prompt_tokens=resp.usage.get("prompt_tokens", 0),
                completion_tokens=resp.usage.get("completion_tokens", 0),
                status="json_fail", error_message=f"json: {exc}"[:500],
            )
            conn.commit()
        return Phase5Result(row.decision_id, "json_fail",
                            latency_s=resp.latency_s, error=str(exc)[:200])

    obj = canonicalise_bge(obj)
    errors = validate_full(obj) if not use_light_prompt else []
    schema_status = "ok"

    # Per-chunk citation extraction (regex only — LLM basis is in decision_enrichment)
    pqs = obj.get("principle_questions", []) or []
    trs = obj.get("prior_case_treatment", []) or []
    total_laws = total_cases = 0

    with pool.connection() as conn:
        _store_decision_enrichment(
            conn, row=row, obj=obj,
            src_hash=src_hash, latency_s=resp.latency_s,
            prompt_tokens=resp.usage.get("prompt_tokens", 0),
            completion_tokens=resp.usage.get("completion_tokens", 0),
            status=schema_status,
        )
        # Clear + re-insert all chunk citations for this decision
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM chunk_law_citations WHERE chunk_id IN "
            "(SELECT id FROM chunks WHERE decision_id=%s)", (row.decision_id,),
        )
        cur.execute(
            "DELETE FROM chunk_case_citations WHERE chunk_id IN "
            "(SELECT id FROM chunks WHERE decision_id=%s)", (row.decision_id,),
        )
        for chunk_id, cleaned in chunk_id_texts:
            laws, cases = resolver.resolve_chunk(
                chunk_text=cleaned,
                llm_legal_basis=[],
                llm_prior_cases=[],
                self_decision_id=row.decision_id,
            )
            if laws:
                cur.executemany(
                    "INSERT INTO chunk_law_citations "
                    "(chunk_id, sr_number, law_abbr, article_num, paragraph, letter, "
                    "raw_text, normalized, source, resolved) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [(chunk_id, c.sr_number, c.law_abbr, c.article_num,
                      c.paragraph, c.letter, c.raw_text, c.normalized,
                      c.source, bool(c.resolved)) for c in laws],
                )
            if cases:
                cur.executemany(
                    "INSERT INTO chunk_case_citations "
                    "(chunk_id, target_decision_id, citation_type, raw_text, source, direction) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    [(chunk_id, c.target_decision_id, c.citation_type,
                      c.raw_text, c.source, c.direction) for c in cases],
                )
            total_laws += len(laws)
            total_cases += len(cases)
        cur.close()
        conn.commit()

    return Phase5Result(
        row.decision_id,
        "ok" if not errors else "schema_invalid",
        latency_s=resp.latency_s,
        prompt_tokens=resp.usage.get("prompt_tokens", 0),
        completion_tokens=resp.usage.get("completion_tokens", 0),
        n_principle_questions=len(pqs),
        n_prior_treatment=len(trs),
        n_law_citations=total_laws,
        n_case_citations=total_cases,
    )


# ---------------------------------------------------------------------------
# Pool runner
# ---------------------------------------------------------------------------

def run_many(
    rows: list[Phase5Row],
    *,
    pool: ConnectionPool,
    client: OpenRouterClient,
    resolver: CitationResolver,
    n_workers: int = 8,
    progress_every: int = 50,
    use_light_prompt: bool = False,
    force: bool = False,
) -> dict:
    agg = {
        "total": len(rows), "ok": 0, "skipped": 0, "error": 0,
        "json_fail": 0, "schema_invalid": 0,
        "latency_sum": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
        "principle_questions": 0, "prior_treatment": 0,
        "law_citations": 0, "case_citations": 0,
    }
    t_start = time.monotonic()

    def work(row: Phase5Row) -> Phase5Result:
        return process_decision(
            row, pool=pool,
            client=client, resolver=resolver,
            use_light_prompt=use_light_prompt, force=force,
        )

    from search_stack.parag.openrouter_client import BudgetExhaustedError

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = [executor.submit(work, r) for r in rows]
        done = 0
        for fut in as_completed(futures):
            try:
                r = fut.result()
            except BudgetExhaustedError as e:
                # FATAL: cancel all remaining futures and exit
                for f in futures:
                    f.cancel()
                print(f"\n  *** BUDGET EXHAUSTED — stopping all workers ***\n  {e}",
                      file=sys.stderr, flush=True)
                agg["budget_exhausted"] = True
                break
            done += 1
            agg[r.status] = agg.get(r.status, 0) + 1
            agg["latency_sum"] += r.latency_s
            agg["prompt_tokens"] += r.prompt_tokens
            agg["completion_tokens"] += r.completion_tokens
            agg["principle_questions"] += r.n_principle_questions
            agg["prior_treatment"] += r.n_prior_treatment
            agg["law_citations"] += r.n_law_citations
            agg["case_citations"] += r.n_case_citations

            if done % progress_every == 0 or done == len(rows):
                elapsed = time.monotonic() - t_start
                rate = done / elapsed if elapsed > 0 else 0
                eta = (len(rows) - done) / rate if rate > 0 else 0
                # Rough cost estimate @ gemini-2.0-flash-001 prices
                cost = (
                    agg["prompt_tokens"] * 0.10
                    + agg["completion_tokens"] * 0.40
                ) / 1_000_000
                print(
                    f"  [{done}/{len(rows)}] "
                    f"ok={agg['ok']} skip={agg['skipped']} err={agg['error']} "
                    f"jfail={agg['json_fail']} sinv={agg['schema_invalid']} "
                    f"| rate={rate*60:.1f}/min eta={eta/60:.1f}min "
                    f"| tok {agg['prompt_tokens']/1000:.0f}k/{agg['completion_tokens']/1000:.0f}k "
                    f"cost≈${cost:.2f}",
                    file=sys.stderr, flush=True,
                )

    agg["wall_time_s"] = time.monotonic() - t_start
    return agg
