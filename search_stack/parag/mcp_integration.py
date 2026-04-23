"""PA-RAG integration layer for the main MCP server.

Provides Postgres-backed functions that the MCP server can call to enrich
its results with PA-RAG signals (authority, validity, chunks, enrichments,
vector search).

Import and call from mcp_server.py — no changes needed to the MCP
protocol layer, just enhanced data behind the same tools.
"""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache

import psycopg

log = logging.getLogger("mcp-parag")

_pg_conn = None


def _get_pg() -> psycopg.Connection | None:
    """Get a Postgres connection, or None if not configured."""
    global _pg_conn
    from search_stack.parag.pg_conn import _load_env
    _load_env()
    pg_url = os.environ.get("CASELAW_PG_URL", "")
    if not pg_url:
        return None
    try:
        if _pg_conn is None or _pg_conn.closed:
            _pg_conn = psycopg.connect(pg_url, autocommit=True)
            log.info("PA-RAG Postgres connected")
        return _pg_conn
    except Exception as e:
        log.warning("PA-RAG Postgres unavailable: %s", e)
        return None


def is_available() -> bool:
    return _get_pg() is not None


# ---------------------------------------------------------------------------
# Enrichment: authority + validity for a list of decision_ids
# ---------------------------------------------------------------------------

def get_authority_batch(decision_ids: list[str]) -> dict[str, dict]:
    """Return {decision_id: {court_level, authority_score, validity_status, ...}}."""
    pg = _get_pg()
    if not pg or not decision_ids:
        return {}
    placeholders = ",".join(["%s"] * len(decision_ids))
    rows = pg.execute(f"""
        SELECT decision_id, court_level, atf_published, authority_score,
               pagerank_temporal, validity_status,
               n_overruled_by, n_criticized_by, n_confirmed_by, n_cited_by
        FROM decision_authority
        WHERE decision_id IN ({placeholders})
    """, decision_ids).fetchall()
    return {
        r[0]: {
            "court_level": r[1], "atf_published": r[2],
            "authority_score": r[3], "pagerank_temporal": r[4],
            "validity_status": r[5],
            "n_overruled_by": r[6], "n_criticized_by": r[7],
            "n_confirmed_by": r[8], "n_cited_by": r[9],
        }
        for r in rows
    }


def get_enrichment(decision_id: str) -> dict | None:
    """Get Phase 5 enrichment for a single decision."""
    pg = _get_pg()
    if not pg:
        return None
    row = pg.execute(
        """SELECT procedural_stage, outcome, subject_matter,
                  principle_questions, obiter_dicta, doctrine_discussion,
                  language_detected, status
           FROM decision_enrichment WHERE decision_id = %s AND status = 'ok'""",
        (decision_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "procedural_stage": row[0], "outcome": row[1],
        "subject_matter": row[2],
        "principle_questions": row[3],  # JSONB → list
        "obiter_dicta": row[4],         # JSONB → list
        "doctrine_discussion": row[5],  # JSONB → dict
        "language_detected": row[6],
    }


def get_chunks_for_decision(decision_id: str) -> list[dict]:
    """Get SAC-enriched chunks for a decision."""
    pg = _get_pg()
    if not pg:
        return []
    rows = pg.execute(
        """SELECT considerant_number, depth, summary, summary_source,
                  left(cleaned, 500) AS snippet, span_start, span_end
           FROM chunks WHERE decision_id = %s ORDER BY span_start""",
        (decision_id,),
    ).fetchall()
    return [
        {"considerant": r[0], "depth": r[1], "summary": r[2],
         "summary_source": r[3], "snippet": r[4],
         "span_start": r[5], "span_end": r[6]}
        for r in rows
    ]


def get_law_citations(decision_id: str) -> list[dict]:
    """Get law citations extracted from chunks of this decision."""
    pg = _get_pg()
    if not pg:
        return []
    rows = pg.execute(
        """SELECT DISTINCT clc.law_abbr, clc.article_num, clc.paragraph,
                  clc.normalized, clc.sr_number, clc.resolved
           FROM chunk_law_citations clc
           JOIN chunks c ON c.id = clc.chunk_id
           WHERE c.decision_id = %s
           ORDER BY clc.law_abbr, clc.article_num""",
        (decision_id,),
    ).fetchall()
    return [
        {"law": r[0], "article": r[1], "paragraph": r[2],
         "normalized": r[3], "sr_number": r[4], "resolved": r[5]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Hybrid search (FTS + vector + authority reranking)
# ---------------------------------------------------------------------------

_embedder = None

def _get_embedder():
    global _embedder
    if _embedder is None:
        from search_stack.parag.embedder import load_embedder
        _embedder = load_embedder(device="cpu")
        log.info("BGE-M3 embedder loaded for hybrid search")
    return _embedder


def hybrid_search(
    query: str,
    language: str | None = None,
    courts: list[str] | None = None,
    court_level_min: int | None = None,
    exclude_overruled: bool = True,
    top_n: int = 10,
) -> list[dict] | None:
    """Run PA-RAG hybrid retrieval. Returns None if Postgres unavailable."""
    pg = _get_pg()
    if not pg:
        return None
    from search_stack.parag.retrieval import RetrievalFilters, retrieve
    model = _get_embedder()
    query_vec = model.encode(query, normalize_embeddings=True, convert_to_numpy=True)
    filters = RetrievalFilters(
        language=language,
        courts=tuple(courts) if courts else None,
        court_level_min=court_level_min,
        exclude_overruled=exclude_overruled,
    )
    results = retrieve(pg, query=query, query_vec=query_vec,
                       filters=filters, top_n=top_n)
    out = []
    for r in results:
        meta = pg.execute(
            "SELECT decision_date, title, docket_number FROM decisions WHERE decision_id = %s",
            (r.decision_id,),
        ).fetchone()
        out.append({
            "decision_id": r.decision_id,
            "court": r.court, "language": r.language,
            "date": str(meta[0]) if meta else None,
            "title": meta[1] if meta else None,
            "docket_number": meta[2] if meta else None,
            "considerant": r.considerant_number,
            "summary": r.summary, "snippet": r.cleaned_snippet,
            "final_score": round(r.final_score, 4),
            "cosine_similarity": round(r.cosine, 4) if r.cosine else None,
            "authority_score": round(r.authority_score, 3),
            "court_level": r.court_level,
            "validity_status": r.validity_status,
            "breakdown": r.breakdown,
        })
    return out


def find_related_decisions(decision_id: str, top_n: int = 5) -> list[dict] | None:
    """Find semantically related decisions via vector similarity."""
    pg = _get_pg()
    if not pg:
        return None
    row = pg.execute(
        """SELECT ce.embedding FROM chunk_embeddings ce
           JOIN chunks c ON c.id = ce.chunk_id
           WHERE c.decision_id = %s LIMIT 1""",
        (decision_id,),
    ).fetchone()
    if not row:
        return []
    vec_text = str(row[0])
    rows = pg.execute(
        """SELECT DISTINCT ON (c.decision_id)
                  c.decision_id, c.court, c.summary,
                  1 - (ce.embedding <=> %s::vector) AS similarity,
                  d.decision_date, d.docket_number, d.title
           FROM chunk_embeddings ce
           JOIN chunks c ON c.id = ce.chunk_id
           LEFT JOIN decisions d ON d.decision_id = c.decision_id
           WHERE c.decision_id != %s
           ORDER BY c.decision_id, ce.embedding <=> %s::vector
           LIMIT %s""",
        (vec_text, decision_id, vec_text, top_n * 3),
    ).fetchall()
    seen = set()
    out = []
    for r in sorted(rows, key=lambda x: x[3], reverse=True):
        if r[0] in seen:
            continue
        seen.add(r[0])
        out.append({
            "decision_id": r[0], "court": r[1], "summary": r[2],
            "similarity": round(r[3], 4),
            "date": str(r[4]) if r[4] else None,
            "docket": r[5], "title": r[6],
        })
        if len(out) >= top_n:
            break
    return out


def generate_exam_question_context(topic: str, language: str = "de") -> dict | None:
    """Get rich context for exam question generation: top chunks + enrichments."""
    results = hybrid_search(topic, language=language, top_n=5, court_level_min=4)
    if not results:
        return None
    context = {"topic": topic, "decisions": []}
    for r in results:
        enrichment = get_enrichment(r["decision_id"])
        context["decisions"].append({
            **r,
            "enrichment": enrichment,
            "law_citations": get_law_citations(r["decision_id"]),
        })
    return context


def get_pg_statistics() -> dict | None:
    """Get PA-RAG pipeline statistics from Postgres."""
    pg = _get_pg()
    if not pg:
        return None
    stats = {}
    for table in ["decisions", "chunks", "chunk_embeddings", "decision_enrichment",
                   "decision_authority", "decision_citations", "decision_statutes",
                   "chunk_law_citations", "chunk_case_citations",
                   "laws_federal", "articles_federal", "laws_cantonal", "articles_cantonal"]:
        stats[table] = pg.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    stats["enrichment_ok"] = pg.execute(
        "SELECT count(*) FROM decision_enrichment WHERE status = 'ok'"
    ).fetchone()[0]
    return stats
