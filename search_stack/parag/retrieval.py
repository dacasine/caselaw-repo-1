"""PA-RAG hybrid retrieval — Phase 7.

Four signals combined per chunk:

    1. Semantic similarity (vector cosine, sqlite-vec)
    2. Lexical match (BM25 over chunks.cleaned + summary via FTS5)
    3. Authority score (decision_authority.court_level × atf × validity)
    4. PageRank temporel (decision_authority.pagerank_temporal)

Pipeline:

    user query
        ├── BM25  top-K_bm25 candidates
        └── ANN   top-K_ann  candidates
               ↓
            RRF fusion (Reciprocal Rank Fusion)  →  top-K_candidates (≈50)
               ↓
          Authority rerank (composite score)
               ↓
            top-N results  (default 10)

Filters applied BEFORE retrieval via SQL:
    - language (de | fr | it)
    - court (substring or in list)
    - court_level_min
    - chunk_type (if annotated)
    - validity_status (exclude 'overruled' by default)
    - date range (decision_date on parent decision)

Output: list of ranked chunks with all scoring components, so the
caller (MCP tool, REST endpoint, evaluation harness) can display or
introspect without another query.
"""

from __future__ import annotations

import sqlite3
import struct
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import sqlite_vec

from db_schema_parag import DEFAULT_PARAG_DB, EMBEDDING_DIM


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_K_BM25 = 100
DEFAULT_K_ANN  = 100
DEFAULT_RRF_K  = 60
DEFAULT_TOP_N  = 10

# Composite authority-rerank weights (see Harvard Law § 2.5).
# Sum to 1.0; tuned later on the Phase 9 benchmark.
DEFAULT_WEIGHTS = {
    "text":     0.40,  # RRF rank score (lexical + semantic)
    "pagerank": 0.25,
    "court":    0.25,
    "validity": 0.10,
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RetrievedChunk:
    chunk_id: int
    decision_id: str
    court: str
    language: str
    considerant_number: str
    summary: str | None
    cleaned_snippet: str        # first ~300 chars for preview
    span_start: int
    span_end: int

    # Scoring components (all normalised to [0,1] after fusion)
    rrf_score: float = 0.0
    bm25_rank: int | None = None
    ann_rank:  int | None = None
    cosine:    float | None = None
    bm25_score: float | None = None

    # Authority-time
    court_level: int = 0
    atf_published: bool = False
    validity_status: str = "valid"
    pagerank_temporal: float = 0.0
    authority_score: float = 0.0   # static base

    # Final composite
    final_score: float = 0.0

    # Optional: per-component contributions (for debug / explainability)
    breakdown: dict[str, float] = field(default_factory=dict)


@dataclass
class RetrievalFilters:
    language: str | None = None
    courts: tuple[str, ...] | None = None
    court_level_min: int | None = None
    chunk_types: tuple[str, ...] | None = None
    exclude_overruled: bool = True
    date_from: str | None = None          # ISO date
    date_to: str | None = None


# ---------------------------------------------------------------------------
# Connection setup
# ---------------------------------------------------------------------------

def open_retrieval_db(path: Path | str = DEFAULT_PARAG_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    # Attach decisions.db read-only for dates/title joins
    src = Path.home() / ".swiss-caselaw" / "decisions.db"
    if src.exists():
        conn.execute(f"ATTACH DATABASE 'file:{src}?mode=ro' AS src KEY '' ")  # URI OK
    conn.row_factory = sqlite3.Row
    return conn


def _vec_blob(v) -> bytes:
    if hasattr(v, "tolist"):
        v = v.tolist()
    return struct.pack(f"{len(v)}f", *v)


# ---------------------------------------------------------------------------
# Filter → SQL fragment
# ---------------------------------------------------------------------------

def _build_where(filters: RetrievalFilters) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []
    if filters.language:
        clauses.append("c.language = ?")
        params.append(filters.language)
    if filters.courts:
        clauses.append(
            "c.court IN (" + ",".join(["?"] * len(filters.courts)) + ")"
        )
        params.extend(filters.courts)
    if filters.chunk_types:
        clauses.append(
            "c.summary_source IN (" + ",".join(["?"] * len(filters.chunk_types)) + ")"
        )
        params.extend(filters.chunk_types)
    if filters.court_level_min is not None:
        clauses.append(
            "(SELECT court_level FROM decision_authority a WHERE a.decision_id = c.decision_id) >= ?"
        )
        params.append(filters.court_level_min)
    if filters.exclude_overruled:
        clauses.append("""
            COALESCE(
                (SELECT validity_status FROM decision_authority a
                 WHERE a.decision_id = c.decision_id), 'valid'
            ) != 'overruled'
        """)
    return (" AND ".join(clauses) if clauses else "1=1"), params


# ---------------------------------------------------------------------------
# BM25 candidates (FTS5)
# ---------------------------------------------------------------------------

def ensure_fts5_index(conn: sqlite3.Connection) -> None:
    """Create the chunks FTS5 virtual table if missing. Must be called on a
    read/write connection — the regular retrieval connection is read-only.
    """
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
        USING fts5(cleaned, summary, content='chunks', content_rowid='id',
                   tokenize='unicode61 remove_diacritics 2')
    """)
    # Triggers to keep it in sync with chunks
    conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, cleaned, summary)
            VALUES (new.id, new.cleaned, new.summary);
        END;
        CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, cleaned, summary)
            VALUES ('delete', old.id, old.cleaned, old.summary);
        END;
        CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, cleaned, summary)
            VALUES ('delete', old.id, old.cleaned, old.summary);
            INSERT INTO chunks_fts(rowid, cleaned, summary)
            VALUES (new.id, new.cleaned, new.summary);
        END;
    """)


def bm25_candidates(
    conn: sqlite3.Connection, query: str, filters: RetrievalFilters,
    k: int = DEFAULT_K_BM25,
) -> list[tuple[int, float]]:
    """Return [(chunk_id, bm25_score)] for top-k BM25 hits."""
    where, params = _build_where(filters)
    fts_query = _escape_for_fts5(query)
    sql = f"""
        SELECT c.id AS chunk_id, bm25(chunks_fts) AS score
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        WHERE chunks_fts MATCH ? AND {where}
        ORDER BY score LIMIT ?
    """
    rows = conn.execute(sql, [fts_query] + params + [k]).fetchall()
    # bm25() returns a negative score (SQLite convention); lower is better.
    # Convert to positive by negation for easier mental model.
    return [(r["chunk_id"], -r["score"]) for r in rows]


_FTS_SPECIAL = '"*:{}()'

def _escape_for_fts5(q: str) -> str:
    """FTS5 MATCH needs queries quoted to avoid syntax errors on punctuation.
    Wrap each token in double quotes, join with AND."""
    tokens = [t.strip() for t in q.split() if t.strip()]
    return " AND ".join(f'"{t.replace("\"", "")}"' for t in tokens)


# ---------------------------------------------------------------------------
# ANN candidates (sqlite-vec)
# ---------------------------------------------------------------------------

def ann_candidates(
    conn: sqlite3.Connection, query_vec, filters: RetrievalFilters,
    k: int = DEFAULT_K_ANN,
) -> list[tuple[int, float]]:
    """Return [(chunk_id, cosine_similarity)] for top-k ANN hits."""
    where, params = _build_where(filters)
    sql = f"""
        SELECT v.rowid AS chunk_id, v.distance AS distance
        FROM vec_chunks v
        JOIN chunks c ON c.id = v.rowid
        WHERE v.embedding MATCH ? AND k = ? AND {where}
        ORDER BY v.distance
    """
    rows = conn.execute(sql, [_vec_blob(query_vec), k] + params).fetchall()
    # sqlite-vec returns squared L2; for normalised vectors cos = 1 - L2²/2.
    return [(r["chunk_id"], 1.0 - r["distance"] / 2.0) for r in rows]


# ---------------------------------------------------------------------------
# RRF fusion
# ---------------------------------------------------------------------------

def rrf_fuse(
    lists: Iterable[list[tuple[int, float]]],
    k: int = DEFAULT_RRF_K,
) -> dict[int, float]:
    """Reciprocal Rank Fusion. `lists` is an iterable of ranked result
    lists (first element = rank 1). Returns {chunk_id: score}."""
    scores: dict[int, float] = defaultdict(float)
    for ranked in lists:
        for rank, (cid, _) in enumerate(ranked, start=1):
            scores[cid] += 1.0 / (k + rank)
    return scores


# ---------------------------------------------------------------------------
# Full retrieval
# ---------------------------------------------------------------------------

def retrieve(
    conn: sqlite3.Connection,
    *,
    query: str,
    query_vec,
    filters: RetrievalFilters | None = None,
    k_bm25: int = DEFAULT_K_BM25,
    k_ann:  int = DEFAULT_K_ANN,
    top_n:  int = DEFAULT_TOP_N,
    weights: dict[str, float] | None = None,
) -> list[RetrievedChunk]:
    """End-to-end retrieval. Caller supplies both the plain-text query
    (for BM25) and its BGE-M3 embedding (for ANN)."""
    filters = filters or RetrievalFilters()
    weights = weights or DEFAULT_WEIGHTS

    bm25 = bm25_candidates(conn, query, filters, k=k_bm25)
    ann  = ann_candidates(conn, query_vec, filters, k=k_ann)
    rrf  = rrf_fuse([bm25, ann])

    if not rrf:
        return []

    # Load ranking signals + metadata for all candidates in one query
    candidate_ids = list(rrf.keys())
    placeholders = ",".join(["?"] * len(candidate_ids))
    meta_rows = conn.execute(f"""
        SELECT c.id, c.decision_id, c.court, c.language,
               c.considerant_number, c.summary, c.cleaned,
               c.span_start, c.span_end,
               COALESCE(a.court_level, 2)       AS court_level,
               COALESCE(a.atf_published, 0)     AS atf_published,
               COALESCE(a.validity_status, 'valid') AS validity_status,
               COALESCE(a.pagerank_temporal, 0.0)  AS pagerank_temporal,
               COALESCE(a.authority_score, 0.30)   AS authority_score
        FROM chunks c
        LEFT JOIN decision_authority a ON a.decision_id = c.decision_id
        WHERE c.id IN ({placeholders})
    """, candidate_ids).fetchall()

    # BM25 and ANN lookups
    bm25_ranks = {cid: i for i, (cid, _) in enumerate(bm25, start=1)}
    ann_ranks  = {cid: i for i, (cid, _) in enumerate(ann,  start=1)}
    bm25_scores = dict(bm25)
    ann_scores  = dict(ann)

    # Normalise pagerank to [0,1] via simple max-scale across candidates
    max_pr = max((r["pagerank_temporal"] for r in meta_rows), default=1.0)
    if max_pr <= 0:
        max_pr = 1.0

    # Validity multiplier
    VAL_MULT = {"valid": 1.0, "distinguished": 0.75, "criticized": 0.5, "overruled": 0.1}

    results: list[RetrievedChunk] = []
    for r in meta_rows:
        cid = r["id"]
        # Normalise RRF to [0,1] using max score in set
        rrf_raw = rrf[cid]
        # Text (rrf) normalisation against the best score in this set
        max_rrf = max(rrf.values())
        rrf_norm = rrf_raw / max_rrf if max_rrf > 0 else 0.0

        pr_norm = r["pagerank_temporal"] / max_pr
        val_mul = VAL_MULT.get(r["validity_status"], 1.0)

        text_s    = weights["text"]     * rrf_norm
        prank_s   = weights["pagerank"] * pr_norm
        court_s   = weights["court"]    * r["authority_score"]
        valid_s   = weights["validity"] * val_mul
        final     = (text_s + prank_s + court_s) * val_mul  # validity as multiplicative gate
        # Keep additive `valid_s` only for debug breakdown.

        results.append(RetrievedChunk(
            chunk_id=cid,
            decision_id=r["decision_id"],
            court=r["court"],
            language=r["language"],
            considerant_number=r["considerant_number"],
            summary=r["summary"],
            cleaned_snippet=(r["cleaned"] or "")[:400],
            span_start=r["span_start"],
            span_end=r["span_end"],
            rrf_score=rrf_norm,
            bm25_rank=bm25_ranks.get(cid),
            ann_rank=ann_ranks.get(cid),
            cosine=ann_scores.get(cid),
            bm25_score=bm25_scores.get(cid),
            court_level=r["court_level"],
            atf_published=bool(r["atf_published"]),
            validity_status=r["validity_status"],
            pagerank_temporal=r["pagerank_temporal"],
            authority_score=r["authority_score"],
            final_score=final,
            breakdown={
                "text":      text_s,
                "pagerank":  prank_s,
                "court":     court_s,
                "validity_mul": val_mul,
            },
        ))

    results.sort(key=lambda x: x.final_score, reverse=True)
    return results[:top_n]
