"""PA-RAG hybrid retrieval — Phase 7 (Postgres + pgvector).

Four signals combined per chunk:

    1. Semantic similarity (vector cosine, pgvector DiskANN)
    2. Lexical match (tsvector FTS on chunks.fts)
    3. Authority score (decision_authority.court_level × atf × validity)
    4. PageRank temporel (decision_authority.pagerank_temporal)

Pipeline:

    user query
        ├── FTS   top-K_fts  candidates  (ts_rank_cd on chunks.fts)
        └── ANN   top-K_ann  candidates  (pgvector cosine distance)
               ↓
            RRF fusion (Reciprocal Rank Fusion)  →  top-K_candidates
               ↓
          Authority rerank (composite score)
               ↓
            top-N results  (default 10)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

import psycopg

from db_schema_parag import EMBEDDING_DIM


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_K_FTS  = 100
DEFAULT_K_ANN  = 100
DEFAULT_RRF_K  = 60
DEFAULT_TOP_N  = 10

DEFAULT_WEIGHTS = {
    "text":     0.40,
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
    cleaned_snippet: str
    span_start: int
    span_end: int

    rrf_score: float = 0.0
    fts_rank: int | None = None
    ann_rank: int | None = None
    cosine: float | None = None
    fts_score: float | None = None

    court_level: int = 0
    atf_published: bool = False
    validity_status: str = "valid"
    pagerank_temporal: float = 0.0
    authority_score: float = 0.0

    final_score: float = 0.0
    breakdown: dict[str, float] = field(default_factory=dict)


@dataclass
class RetrievalFilters:
    language: str | None = None
    courts: tuple[str, ...] | None = None
    court_level_min: int | None = None
    exclude_overruled: bool = True
    date_from: str | None = None
    date_to: str | None = None


# ---------------------------------------------------------------------------
# Filter → SQL fragment
# ---------------------------------------------------------------------------

def _build_where(filters: RetrievalFilters) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []
    if filters.language:
        clauses.append("c.language = %s")
        params.append(filters.language)
    if filters.courts:
        placeholders = ",".join(["%s"] * len(filters.courts))
        clauses.append(f"c.court IN ({placeholders})")
        params.extend(filters.courts)
    if filters.court_level_min is not None:
        clauses.append(
            "(SELECT court_level FROM decision_authority a "
            "WHERE a.decision_id = c.decision_id) >= %s"
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
# FTS candidates (tsvector)
# ---------------------------------------------------------------------------

def fts_candidates(
    conn: psycopg.Connection, query: str, filters: RetrievalFilters,
    k: int = DEFAULT_K_FTS,
) -> list[tuple[int, float]]:
    """Return [(chunk_id, fts_score)] for top-k FTS hits using Postgres tsvector."""
    where, params = _build_where(filters)
    tsquery = " & ".join(
        f"'{t}'" for t in query.split() if t.strip()
    )
    if not tsquery:
        return []
    sql = f"""
        SELECT c.id AS chunk_id,
               ts_rank_cd(c.fts, to_tsquery('simple', %s)) AS score
        FROM chunks c
        WHERE c.fts @@ to_tsquery('simple', %s) AND {where}
        ORDER BY score DESC
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, [tsquery, tsquery] + params + [k])
        return [(r[0], r[1]) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# ANN candidates (pgvector DiskANN)
# ---------------------------------------------------------------------------

def _vec_to_text(v) -> str:
    if hasattr(v, "tolist"):
        v = v.tolist()
    return "[" + ",".join(str(x) for x in v) + "]"


def ann_candidates(
    conn: psycopg.Connection, query_vec, filters: RetrievalFilters,
    k: int = DEFAULT_K_ANN,
) -> list[tuple[int, float]]:
    """Return [(chunk_id, cosine_similarity)] for top-k ANN hits via pgvector."""
    where, filter_params = _build_where(filters)
    vec_text = _vec_to_text(query_vec)
    sql = f"""
        SELECT ce.chunk_id,
               1 - (ce.embedding <=> %s::vector) AS cosine_sim
        FROM chunk_embeddings ce
        JOIN chunks c ON c.id = ce.chunk_id
        WHERE {where}
        ORDER BY ce.embedding <=> %s::vector
        LIMIT %s
    """
    all_params = [vec_text] + filter_params + [vec_text, k]
    with conn.cursor() as cur:
        cur.execute(sql, all_params)
        return [(r[0], r[1]) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# RRF fusion
# ---------------------------------------------------------------------------

def rrf_fuse(
    lists: Iterable[list[tuple[int, float]]],
    k: int = DEFAULT_RRF_K,
) -> dict[int, float]:
    scores: dict[int, float] = defaultdict(float)
    for ranked in lists:
        for rank, (cid, _) in enumerate(ranked, start=1):
            scores[cid] += 1.0 / (k + rank)
    return scores


# ---------------------------------------------------------------------------
# Full retrieval
# ---------------------------------------------------------------------------

def retrieve(
    conn: psycopg.Connection,
    *,
    query: str,
    query_vec,
    filters: RetrievalFilters | None = None,
    k_fts: int = DEFAULT_K_FTS,
    k_ann: int = DEFAULT_K_ANN,
    top_n: int = DEFAULT_TOP_N,
    weights: dict[str, float] | None = None,
) -> list[RetrievedChunk]:
    filters = filters or RetrievalFilters()
    weights = weights or DEFAULT_WEIGHTS

    fts = fts_candidates(conn, query, filters, k=k_fts)
    ann = ann_candidates(conn, query_vec, filters, k=k_ann)
    rrf = rrf_fuse([fts, ann])

    if not rrf:
        return []

    candidate_ids = list(rrf.keys())
    placeholders = ",".join(["%s"] * len(candidate_ids))
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT c.id, c.decision_id, c.court, c.language,
                   c.considerant_number, c.summary,
                   left(c.cleaned, 400) AS cleaned_snippet,
                   c.span_start, c.span_end,
                   COALESCE(a.court_level, 2) AS court_level,
                   COALESCE(a.atf_published, false) AS atf_published,
                   COALESCE(a.validity_status, 'valid') AS validity_status,
                   COALESCE(a.pagerank_temporal, 0.0) AS pagerank_temporal,
                   COALESCE(a.authority_score, 0.30) AS authority_score
            FROM chunks c
            LEFT JOIN decision_authority a ON a.decision_id = c.decision_id
            WHERE c.id IN ({placeholders})
        """, candidate_ids)
        meta_rows = cur.fetchall()

    fts_ranks = {cid: i for i, (cid, _) in enumerate(fts, start=1)}
    ann_ranks = {cid: i for i, (cid, _) in enumerate(ann, start=1)}
    fts_scores = dict(fts)
    ann_scores = dict(ann)

    max_pr = max((r[12] for r in meta_rows), default=1.0) or 1.0

    VAL_MULT = {"valid": 1.0, "distinguished": 0.75, "criticized": 0.5, "overruled": 0.1}
    max_rrf = max(rrf.values()) if rrf else 1.0

    results: list[RetrievedChunk] = []
    for r in meta_rows:
        cid = r[0]
        rrf_norm = rrf[cid] / max_rrf if max_rrf > 0 else 0.0
        pr_norm = r[12] / max_pr
        val_mul = VAL_MULT.get(r[11], 1.0)

        text_s  = weights["text"]     * rrf_norm
        prank_s = weights["pagerank"] * pr_norm
        court_s = weights["court"]    * r[13]
        final   = (text_s + prank_s + court_s) * val_mul

        results.append(RetrievedChunk(
            chunk_id=cid, decision_id=r[1], court=r[2], language=r[3],
            considerant_number=r[4], summary=r[5],
            cleaned_snippet=r[6] or "",
            span_start=r[7], span_end=r[8],
            rrf_score=rrf_norm,
            fts_rank=fts_ranks.get(cid),
            ann_rank=ann_ranks.get(cid),
            cosine=ann_scores.get(cid),
            fts_score=fts_scores.get(cid),
            court_level=r[9],
            atf_published=bool(r[10]),
            validity_status=r[11],
            pagerank_temporal=r[12],
            authority_score=r[13],
            final_score=final,
            breakdown={"text": text_s, "pagerank": prank_s,
                       "court": court_s, "validity_mul": val_mul},
        ))

    results.sort(key=lambda x: x.final_score, reverse=True)
    return results[:top_n]
