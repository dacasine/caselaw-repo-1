"""
PA-RAG MCP Server — Postgres-backed Swiss case-law retrieval (22 tools).

All data served from Postgres + pgvector. Hybrid search (FTS + vector +
authority reranking) via search_stack.parag.retrieval.

Usage:
    claude mcp add pa-rag -- python3 /path/to/mcp_parag.py          # stdio
    MCP_TRANSPORT=sse MCP_PORT=8001 python3 /path/to/mcp_parag.py   # SSE
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg
from search_stack.parag.pg_conn import get_pg_url
from search_stack.parag.retrieval import RetrievalFilters, retrieve

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mcp-parag")

# ---------------------------------------------------------------------------
# Lazy singletons
# ---------------------------------------------------------------------------

_conn: psycopg.Connection | None = None
_embedder = None


def _get_conn() -> psycopg.Connection:
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg.connect(get_pg_url(), autocommit=True)
        log.info("Connected to Postgres")
    return _conn


def _get_embedder():
    global _embedder
    if _embedder is None:
        from search_stack.parag.embedder import load_embedder
        _embedder = load_embedder(device="cpu")
        log.info("BGE-M3 embedder loaded")
    return _embedder


def _embed_query(query: str):
    return _get_embedder().encode(query, normalize_embeddings=True, convert_to_numpy=True)


def _vec_to_text(v) -> str:
    if hasattr(v, "tolist"):
        v = v.tolist()
    return "[" + ",".join(str(x) for x in v) + "]"


# ---------------------------------------------------------------------------
# Decision ID resolution
# ---------------------------------------------------------------------------

_BGE_RE = re.compile(r"^BGE\s+(\d+)\s+([IV]+)\s+(\d+)$", re.IGNORECASE)


def _resolve_id(raw: str) -> str:
    """Resolve BGE reference or docket number to a decision_id."""
    m = _BGE_RE.match(raw.strip())
    if m:
        return f"bge_BGE_{m.group(1)}_{m.group(2)}_{m.group(3)}"
    conn = _get_conn()
    row = conn.execute(
        "SELECT decision_id FROM decisions WHERE decision_id = %s", (raw,)
    ).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "SELECT decision_id FROM decisions WHERE docket_number = %s LIMIT 1", (raw,)
    ).fetchone()
    if row:
        return row[0]
    return raw


def _not_found(decision_id: str) -> dict:
    return {"error": f"Decision '{decision_id}' not found"}


def _jsonout(obj) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(obj, indent=2, ensure_ascii=False, default=str))]


def _textout(text: str) -> list[TextContent]:
    return [TextContent(type="text", text=text)]


# ---------------------------------------------------------------------------
# 1. search_decisions
# ---------------------------------------------------------------------------

def _search_decisions(query: str, language: str | None = None,
                      courts: list[str] | None = None,
                      court_level_min: int | None = None,
                      exclude_overruled: bool = True, top_n: int = 10) -> list[dict]:
    conn = _get_conn()
    query_vec = _embed_query(query)
    filters = RetrievalFilters(
        language=language,
        courts=tuple(courts) if courts else None,
        court_level_min=court_level_min,
        exclude_overruled=exclude_overruled,
    )
    results = retrieve(conn, query=query, query_vec=query_vec, filters=filters, top_n=top_n)
    out = []
    for r in results:
        meta = conn.execute(
            "SELECT decision_date, title, docket_number FROM decisions WHERE decision_id = %s",
            (r.decision_id,),
        ).fetchone()
        out.append({
            "decision_id": r.decision_id, "court": r.court, "language": r.language,
            "date": str(meta[0]) if meta else None,
            "title": meta[1] if meta else None,
            "docket_number": meta[2] if meta else None,
            "considerant": r.considerant_number,
            "summary": r.summary, "snippet": r.cleaned_snippet,
            "score": round(r.final_score, 4),
            "cosine": round(r.cosine, 4) if r.cosine else None,
            "authority_score": round(r.authority_score, 3),
            "court_level": r.court_level, "validity": r.validity_status,
        })
    return out


# ---------------------------------------------------------------------------
# 2. get_decision
# ---------------------------------------------------------------------------

def _get_decision(raw_id: str) -> dict | None:
    decision_id = _resolve_id(raw_id)
    conn = _get_conn()
    row = conn.execute(
        """SELECT decision_id, court, canton, chamber, docket_number,
                  decision_date, language, title, legal_area, regeste,
                  full_text, source_url
           FROM decisions WHERE decision_id = %s""",
        (decision_id,),
    ).fetchone()
    if not row:
        return None
    cols = ["decision_id", "court", "canton", "chamber", "docket_number",
            "decision_date", "language", "title", "legal_area", "regeste",
            "full_text", "source_url"]
    d = dict(zip(cols, row))
    d["decision_date"] = str(d["decision_date"]) if d["decision_date"] else None
    chunks = conn.execute(
        """SELECT considerant_number, depth, summary, left(cleaned, 500) AS snippet
           FROM chunks WHERE decision_id = %s ORDER BY span_start""",
        (decision_id,),
    ).fetchall()
    d["chunks"] = [{"considerant": c[0], "depth": c[1], "summary": c[2], "snippet": c[3]} for c in chunks]
    return d


# ---------------------------------------------------------------------------
# 3. list_courts
# ---------------------------------------------------------------------------

def _list_courts() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("""
        SELECT court, count(*) AS n,
               min(decision_date) AS first_date,
               max(decision_date) AS last_date,
               array_agg(DISTINCT language) AS languages
        FROM decisions GROUP BY court ORDER BY n DESC
    """).fetchall()
    return [{"court": r[0], "count": r[1], "first_date": str(r[2]) if r[2] else None,
             "last_date": str(r[3]) if r[3] else None, "languages": r[4]} for r in rows]


# ---------------------------------------------------------------------------
# 4. get_statistics
# ---------------------------------------------------------------------------

def _get_statistics(court: str | None = None, canton: str | None = None,
                    year: int | None = None) -> dict:
    conn = _get_conn()
    where_parts, params = [], []
    if court:
        where_parts.append("court = %s"); params.append(court)
    if canton:
        where_parts.append("canton = %s"); params.append(canton)
    if year:
        where_parts.append("extract(year from decision_date) = %s"); params.append(year)
    where = " AND ".join(where_parts) if where_parts else "1=1"

    stats: dict = {}
    stats["total_decisions"] = conn.execute(
        f"SELECT count(*) FROM decisions WHERE {where}", params
    ).fetchone()[0]
    stats["total_chunks"] = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    stats["total_embeddings"] = conn.execute("SELECT count(*) FROM chunk_embeddings").fetchone()[0]
    stats["total_enrichments"] = conn.execute(
        "SELECT count(*) FROM decision_enrichment WHERE status = 'ok'"
    ).fetchone()[0]
    stats["total_citations"] = conn.execute("SELECT count(*) FROM decision_citations").fetchone()[0]

    rows = conn.execute(
        f"SELECT court, count(*) FROM decisions WHERE {where} GROUP BY court ORDER BY count(*) DESC LIMIT 20",
        params,
    ).fetchall()
    stats["courts_top20"] = [{"court": r[0], "count": r[1]} for r in rows]

    rows = conn.execute(
        f"SELECT language, count(*) FROM decisions WHERE {where} GROUP BY language ORDER BY count(*) DESC",
        params,
    ).fetchall()
    stats["by_language"] = {r[0]: r[1] for r in rows}
    return stats


# ---------------------------------------------------------------------------
# 5. find_citations
# ---------------------------------------------------------------------------

def _find_citations(raw_id: str, direction: str = "both", limit: int = 20) -> dict:
    decision_id = _resolve_id(raw_id)
    conn = _get_conn()
    result: dict = {"decision_id": decision_id}
    if direction in ("outbound", "both"):
        rows = conn.execute(
            """SELECT dc.target_decision_id, dc.raw_text, dc.confidence_score,
                      d.court, d.decision_date, d.docket_number
               FROM decision_citations dc
               LEFT JOIN decisions d ON d.decision_id = dc.target_decision_id
               WHERE dc.source_decision_id = %s
               ORDER BY dc.confidence_score DESC NULLS LAST LIMIT %s""",
            (decision_id, limit),
        ).fetchall()
        result["cites"] = [{"decision_id": r[0], "raw_text": r[1], "confidence": r[2],
                            "court": r[3], "date": str(r[4]) if r[4] else None,
                            "docket": r[5]} for r in rows]
    if direction in ("inbound", "both"):
        rows = conn.execute(
            """SELECT dc.source_decision_id, dc.raw_text, dc.confidence_score,
                      d.court, d.decision_date, d.docket_number
               FROM decision_citations dc
               LEFT JOIN decisions d ON d.decision_id = dc.source_decision_id
               WHERE dc.target_decision_id = %s
               ORDER BY d.decision_date DESC NULLS LAST LIMIT %s""",
            (decision_id, limit),
        ).fetchall()
        result["cited_by"] = [{"decision_id": r[0], "raw_text": r[1], "confidence": r[2],
                               "court": r[3], "date": str(r[4]) if r[4] else None,
                               "docket": r[5]} for r in rows]
    return result


# ---------------------------------------------------------------------------
# 6. find_appeal_chain
# ---------------------------------------------------------------------------

def _find_appeal_chain(raw_id: str) -> dict:
    decision_id = _resolve_id(raw_id)
    conn = _get_conn()
    chain = []
    # Trace upward: find decisions that cite this one AND are from a higher court
    current = decision_id
    visited = {current}
    for _ in range(5):
        row = conn.execute(
            """SELECT dc.source_decision_id, d.court, d.decision_date, d.docket_number,
                      da.court_level
               FROM decision_citations dc
               JOIN decisions d ON d.decision_id = dc.source_decision_id
               LEFT JOIN decision_authority da ON da.decision_id = dc.source_decision_id
               WHERE dc.target_decision_id = %s
                 AND dc.source_decision_id NOT IN (SELECT unnest(%s::text[]))
               ORDER BY da.court_level DESC NULLS LAST, d.decision_date DESC
               LIMIT 1""",
            (current, list(visited)),
        ).fetchone()
        if not row:
            break
        chain.append({"decision_id": row[0], "court": row[1],
                       "date": str(row[2]) if row[2] else None,
                       "docket": row[3], "court_level": row[4]})
        visited.add(row[0])
        current = row[0]
    # Trace downward from original
    current = decision_id
    lower = []
    visited_down = {current}
    for _ in range(5):
        row = conn.execute(
            """SELECT dc.target_decision_id, d.court, d.decision_date, d.docket_number,
                      da.court_level
               FROM decision_citations dc
               JOIN decisions d ON d.decision_id = dc.target_decision_id
               LEFT JOIN decision_authority da ON da.decision_id = dc.target_decision_id
               WHERE dc.source_decision_id = %s
                 AND dc.target_decision_id NOT IN (SELECT unnest(%s::text[]))
               ORDER BY da.court_level ASC NULLS LAST, d.decision_date ASC
               LIMIT 1""",
            (current, list(visited_down)),
        ).fetchone()
        if not row:
            break
        lower.append({"decision_id": row[0], "court": row[1],
                       "date": str(row[2]) if row[2] else None,
                       "docket": row[3], "court_level": row[4]})
        visited_down.add(row[0])
        current = row[0]
    # Get info on the starting decision
    start = conn.execute(
        """SELECT d.court, d.decision_date, d.docket_number, da.court_level
           FROM decisions d LEFT JOIN decision_authority da ON da.decision_id = d.decision_id
           WHERE d.decision_id = %s""", (decision_id,),
    ).fetchone()
    start_info = {"decision_id": decision_id, "court": start[0] if start else None,
                  "date": str(start[1]) if start and start[1] else None,
                  "docket": start[2] if start else None,
                  "court_level": start[3] if start else None} if start else {"decision_id": decision_id}
    return {"chain": list(reversed(lower)) + [start_info] + chain,
            "origin_index": len(lower)}


# ---------------------------------------------------------------------------
# 7. find_leading_cases
# ---------------------------------------------------------------------------

def _find_leading_cases(topic: str | None = None, sr_number: str | None = None,
                        article: str | None = None, top_n: int = 10) -> list[dict]:
    conn = _get_conn()
    if sr_number:
        where_parts = ["ds.sr_number = %s"]
        params: list = [sr_number]
        if article:
            where_parts.append("ds.article_num = %s"); params.append(article)
        where = " AND ".join(where_parts)
        rows = conn.execute(
            f"""SELECT d.decision_id, d.court, d.decision_date, d.docket_number, d.title,
                       da.authority_score, da.n_cited_by, da.atf_published, da.validity_status
                FROM decision_statutes ds
                JOIN decisions d ON d.decision_id = ds.source_decision_id
                LEFT JOIN decision_authority da ON da.decision_id = d.decision_id
                WHERE {where}
                ORDER BY COALESCE(da.authority_score, 0) DESC, COALESCE(da.n_cited_by, 0) DESC
                LIMIT %s""",
            params + [top_n],
        ).fetchall()
    elif topic:
        search_results = _search_decisions(topic, top_n=top_n * 3, court_level_min=4)
        ids = [r["decision_id"] for r in search_results]
        if not ids:
            return []
        ph = ",".join(["%s"] * len(ids))
        rows = conn.execute(
            f"""SELECT d.decision_id, d.court, d.decision_date, d.docket_number, d.title,
                       da.authority_score, da.n_cited_by, da.atf_published, da.validity_status
                FROM decisions d
                LEFT JOIN decision_authority da ON da.decision_id = d.decision_id
                WHERE d.decision_id IN ({ph})
                ORDER BY COALESCE(da.authority_score, 0) DESC, COALESCE(da.n_cited_by, 0) DESC
                LIMIT %s""",
            ids + [top_n],
        ).fetchall()
    else:
        return [{"error": "Provide either topic or sr_number"}]
    return [{"decision_id": r[0], "court": r[1], "date": str(r[2]) if r[2] else None,
             "docket": r[3], "title": r[4],
             "authority_score": float(r[5]) if r[5] else None,
             "n_cited_by": r[6], "atf_published": r[7],
             "validity": r[8]} for r in rows]


# ---------------------------------------------------------------------------
# 8. analyze_legal_trend
# ---------------------------------------------------------------------------

def _analyze_legal_trend(topic: str | None = None, sr_number: str | None = None,
                         article: str | None = None) -> dict:
    conn = _get_conn()
    if sr_number:
        where_parts = ["ds.sr_number = %s"]
        params: list = [sr_number]
        if article:
            where_parts.append("ds.article_num = %s"); params.append(article)
        where = " AND ".join(where_parts)
        rows = conn.execute(
            f"""SELECT extract(year from d.decision_date)::int AS yr, count(*) AS n
                FROM decision_statutes ds
                JOIN decisions d ON d.decision_id = ds.source_decision_id
                WHERE {where} AND d.decision_date IS NOT NULL
                GROUP BY yr ORDER BY yr""",
            params,
        ).fetchall()
    elif topic:
        # Use FTS on decisions.fts
        tsquery = " & ".join(f"'{t}'" for t in topic.split() if t.strip())
        if not tsquery:
            return {"error": "Empty topic"}
        rows = conn.execute(
            """SELECT extract(year from decision_date)::int AS yr, count(*) AS n
               FROM decisions
               WHERE fts @@ to_tsquery('simple', %s) AND decision_date IS NOT NULL
               GROUP BY yr ORDER BY yr""",
            (tsquery,),
        ).fetchall()
    else:
        return {"error": "Provide either topic or sr_number"}
    return {"query": topic or f"SR {sr_number} art. {article or '*'}",
            "years": [{"year": r[0], "count": r[1]} for r in rows],
            "total": sum(r[1] for r in rows)}


# ---------------------------------------------------------------------------
# 9. draft_mock_decision
# ---------------------------------------------------------------------------

def _draft_mock_decision(facts: str, language: str = "de", top_n: int = 5) -> dict:
    results = _search_decisions(facts, language=language, top_n=top_n, court_level_min=3)
    conn = _get_conn()
    # Collect statutes from leading cases
    statutes = set()
    for r in results:
        rows = conn.execute(
            """SELECT DISTINCT clc.normalized
               FROM chunk_law_citations clc
               JOIN chunks c ON c.id = clc.chunk_id
               WHERE c.decision_id = %s AND clc.normalized IS NOT NULL""",
            (r["decision_id"],),
        ).fetchall()
        for s in rows:
            statutes.add(s[0])
    return {
        "fact_pattern": facts,
        "language": language,
        "applicable_statutes": sorted(statutes),
        "leading_cases": [{"decision_id": r["decision_id"], "court": r["court"],
                           "date": r["date"], "docket_number": r["docket_number"],
                           "summary": r["summary"], "score": r["score"]}
                          for r in results],
        "analysis_outline": {
            "sachverhalt": "Based on the provided facts",
            "rechtliche_erwägungen": [s for s in sorted(statutes)[:5]],
            "dispositiv": "To be determined based on analysis",
        },
        "clarification_questions": [
            "What is the procedural stage of the dispute?",
            "Which canton or federal jurisdiction applies?",
            "Are there specific treaty obligations involved?",
        ],
    }


# ---------------------------------------------------------------------------
# 10. get_case_brief
# ---------------------------------------------------------------------------

def _get_case_brief(raw_id: str) -> dict | None:
    decision_id = _resolve_id(raw_id)
    conn = _get_conn()
    dec = conn.execute(
        """SELECT decision_id, court, docket_number, decision_date, language,
                  title, legal_area, regeste, source_url
           FROM decisions WHERE decision_id = %s""",
        (decision_id,),
    ).fetchone()
    if not dec:
        return None
    brief: dict = {
        "decision_id": dec[0], "court": dec[1], "docket_number": dec[2],
        "date": str(dec[3]) if dec[3] else None, "language": dec[4],
        "title": dec[5], "legal_area": dec[6], "regeste": dec[7], "source_url": dec[8],
    }
    # Chunks grouped by section
    chunks = conn.execute(
        """SELECT considerant_number, depth, summary, left(cleaned, 800)
           FROM chunks WHERE decision_id = %s ORDER BY span_start""",
        (decision_id,),
    ).fetchall()
    sachverhalt, erwaegungen, dispositiv = [], [], []
    for c in chunks:
        entry = {"considerant": c[0], "depth": c[1], "summary": c[2], "snippet": c[3]}
        cn = (c[0] or "").lower()
        if "sachverhalt" in cn or "faits" in cn or "fatto" in cn:
            sachverhalt.append(entry)
        elif "dispositiv" in cn or "dispositif" in cn:
            dispositiv.append(entry)
        else:
            erwaegungen.append(entry)
    brief["sachverhalt"] = sachverhalt
    brief["erwaegungen"] = erwaegungen[:5]
    brief["dispositiv"] = dispositiv
    # Statutes cited
    laws = conn.execute(
        """SELECT DISTINCT clc.normalized, clc.sr_number
           FROM chunk_law_citations clc JOIN chunks c ON c.id = clc.chunk_id
           WHERE c.decision_id = %s AND clc.normalized IS NOT NULL
           ORDER BY clc.normalized""",
        (decision_id,),
    ).fetchall()
    brief["statutes"] = [{"normalized": r[0], "sr_number": r[1]} for r in laws]
    # Citation counts
    auth = conn.execute(
        "SELECT n_cited_by, n_confirmed_by, n_criticized_by, n_overruled_by FROM decision_authority WHERE decision_id = %s",
        (decision_id,),
    ).fetchone()
    if auth:
        brief["citation_counts"] = {"cited_by": auth[0], "confirmed_by": auth[1],
                                     "criticized_by": auth[2], "overruled_by": auth[3]}
    return brief


# ---------------------------------------------------------------------------
# 11. get_decision_structure
# ---------------------------------------------------------------------------

def _get_decision_structure(raw_id: str) -> dict | None:
    decision_id = _resolve_id(raw_id)
    conn = _get_conn()
    exists = conn.execute("SELECT 1 FROM decisions WHERE decision_id = %s", (decision_id,)).fetchone()
    if not exists:
        return None
    chunks = conn.execute(
        """SELECT considerant_number, depth, summary, left(cleaned, 1000), span_start, span_end
           FROM chunks WHERE decision_id = %s ORDER BY span_start""",
        (decision_id,),
    ).fetchall()
    sections: dict = {}
    for c in chunks:
        cn = c[0] or "other"
        if cn not in sections:
            sections[cn] = []
        sections[cn].append({"depth": c[1], "summary": c[2], "snippet": c[3],
                              "span_start": c[4], "span_end": c[5]})
    return {"decision_id": decision_id, "sections": sections, "total_chunks": len(chunks)}


# ---------------------------------------------------------------------------
# 12. get_erwaegung
# ---------------------------------------------------------------------------

def _get_erwaegung(raw_id: str, considerant_number: str) -> dict | None:
    decision_id = _resolve_id(raw_id)
    conn = _get_conn()
    rows = conn.execute(
        """SELECT id, considerant_number, depth, summary, cleaned, span_start, span_end
           FROM chunks WHERE decision_id = %s AND considerant_number = %s
           ORDER BY span_start""",
        (decision_id, considerant_number),
    ).fetchall()
    if not rows:
        return None
    return {
        "decision_id": decision_id,
        "considerant_number": considerant_number,
        "chunks": [{"chunk_id": r[0], "depth": r[2], "summary": r[3],
                     "text": r[4], "span_start": r[5], "span_end": r[6]} for r in rows],
    }


# ---------------------------------------------------------------------------
# 13. get_regeste
# ---------------------------------------------------------------------------

def _get_regeste(raw_id: str) -> dict | None:
    decision_id = _resolve_id(raw_id)
    conn = _get_conn()
    row = conn.execute(
        "SELECT decision_id, court, docket_number, decision_date, regeste FROM decisions WHERE decision_id = %s",
        (decision_id,),
    ).fetchone()
    if not row:
        return None
    return {"decision_id": row[0], "court": row[1], "docket_number": row[2],
            "date": str(row[3]) if row[3] else None, "regeste": row[4]}


# ---------------------------------------------------------------------------
# 14. get_doctrine
# ---------------------------------------------------------------------------

def _get_doctrine(concept: str, sr_number: str | None = None,
                  article: str | None = None, language: str = "de",
                  top_n: int = 10) -> dict:
    conn = _get_conn()
    result: dict = {"concept": concept, "language": language}
    # Law articles
    if sr_number:
        where_parts = ["sr_number = %s"]
        params: list = [sr_number]
        if article:
            where_parts.append("article_num = %s"); params.append(article)
        if language:
            where_parts.append("lang = %s"); params.append(language)
        articles = conn.execute(
            f"""SELECT sr_number, article_num, heading, left(text, 1000)
                FROM articles_federal WHERE {' AND '.join(where_parts)}
                ORDER BY article_num LIMIT 20""",
            params,
        ).fetchall()
        result["federal_articles"] = [{"sr_number": a[0], "article": a[1],
                                        "heading": a[2], "text": a[3]} for a in articles]
    else:
        # FTS search on articles
        tsquery = " & ".join(f"'{t}'" for t in concept.split() if t.strip())
        if tsquery:
            articles = conn.execute(
                """SELECT sr_number, article_num, heading, left(text, 500),
                          ts_rank_cd(fts, to_tsquery('simple', %s)) AS score
                   FROM articles_federal
                   WHERE fts @@ to_tsquery('simple', %s)
                   ORDER BY score DESC LIMIT 10""",
                (tsquery, tsquery),
            ).fetchall()
            result["federal_articles"] = [{"sr_number": a[0], "article": a[1],
                                            "heading": a[2], "text": a[3],
                                            "score": float(a[4])} for a in articles]
    # Leading cases for this concept
    cases = _search_decisions(concept, language=language, top_n=top_n, court_level_min=4)
    result["leading_cases"] = cases
    # Doctrinal timeline: year distribution
    if sr_number:
        trend = _analyze_legal_trend(sr_number=sr_number, article=article)
        result["timeline"] = trend.get("years", [])
    return result


# ---------------------------------------------------------------------------
# 15. generate_exam_question
# ---------------------------------------------------------------------------

def _generate_exam_question(topic: str, language: str = "de") -> dict:
    results = _search_decisions(topic, language=language, top_n=5, court_level_min=4)
    if not results:
        return {"error": "No relevant BGE found for this topic"}
    best = results[0]
    conn = _get_conn()
    # Get fact chunks
    chunks = conn.execute(
        """SELECT considerant_number, summary, left(cleaned, 600)
           FROM chunks WHERE decision_id = %s ORDER BY span_start""",
        (best["decision_id"],),
    ).fetchall()
    sachverhalt = [c for c in chunks if "sachverhalt" in (c[0] or "").lower()
                   or "faits" in (c[0] or "").lower() or "fatto" in (c[0] or "").lower()]
    erwaegungen = [c for c in chunks if c not in sachverhalt]
    # Get statutes
    laws = conn.execute(
        """SELECT DISTINCT clc.normalized FROM chunk_law_citations clc
           JOIN chunks c ON c.id = clc.chunk_id
           WHERE c.decision_id = %s AND clc.normalized IS NOT NULL""",
        (best["decision_id"],),
    ).fetchall()
    return {
        "source_decision": best["decision_id"],
        "court": best["court"], "date": best["date"],
        "topic": topic,
        "fact_pattern": [{"considerant": s[0], "summary": s[1], "snippet": s[2]} for s in sachverhalt[:3]],
        "hidden_analysis": {
            "erwaegungen_excerpts": [{"considerant": e[0], "summary": e[1]} for e in erwaegungen[:4]],
            "applicable_statutes": [r[0] for r in laws],
            "authority_score": best["authority_score"],
        },
    }


# ---------------------------------------------------------------------------
# 16. get_law
# ---------------------------------------------------------------------------

def _get_law(sr_number: str | None = None, abbreviation: str | None = None,
             article: str | None = None, language: str = "de",
             canton: str | None = None) -> dict:
    conn = _get_conn()
    if canton:
        # Cantonal law
        where_parts, params = [], []
        if sr_number:
            where_parts.append("ac.sr_number = %s"); params.append(sr_number)
        if abbreviation:
            where_parts.append("lc.title ILIKE %s"); params.append(f"%{abbreviation}%")
        if canton:
            where_parts.append("ac.canton = %s"); params.append(canton)
        where_parts.append("ac.language = %s"); params.append(language)
        if article:
            where_parts.append("ac.article_num = %s"); params.append(article)
        where = " AND ".join(where_parts) if where_parts else "1=1"
        rows = conn.execute(
            f"""SELECT ac.lexfind_id, ac.canton, ac.article_num, ac.heading, ac.text,
                       lc.title, lc.sr_number
                FROM articles_cantonal ac
                JOIN laws_cantonal lc ON lc.lexfind_id = ac.lexfind_id AND lc.language = ac.language
                WHERE {where} ORDER BY ac.article_num LIMIT 50""",
            params,
        ).fetchall()
        return {"type": "cantonal", "canton": canton, "articles": [
            {"lexfind_id": r[0], "canton": r[1], "article": r[2], "heading": r[3],
             "text": r[4], "law_title": r[5], "sr_number": r[6]} for r in rows
        ]}
    else:
        # Federal law
        where_parts, params = [], []
        if sr_number:
            where_parts.append("af.sr_number = %s"); params.append(sr_number)
        elif abbreviation:
            where_parts.append(
                "af.sr_number IN (SELECT sr_number FROM laws_federal WHERE abbr_de ILIKE %s OR abbr_fr ILIKE %s OR abbr_it ILIKE %s)"
            )
            params.extend([abbreviation, abbreviation, abbreviation])
        where_parts.append("af.lang = %s"); params.append(language)
        if article:
            where_parts.append("af.article_num = %s"); params.append(article)
        where = " AND ".join(where_parts) if where_parts else "1=1"
        rows = conn.execute(
            f"""SELECT af.sr_number, af.article_num, af.heading, af.text,
                       lf.title_{language} AS law_title, lf.abbr_{language} AS law_abbr
                FROM articles_federal af
                LEFT JOIN laws_federal lf ON lf.sr_number = af.sr_number
                WHERE {where} ORDER BY af.article_num LIMIT 50""",
            params,
        ).fetchall()
        return {"type": "federal", "articles": [
            {"sr_number": r[0], "article": r[1], "heading": r[2], "text": r[3],
             "law_title": r[4], "law_abbr": r[5]} for r in rows
        ]}


# ---------------------------------------------------------------------------
# 17. search_laws
# ---------------------------------------------------------------------------

def _search_laws(query: str, language: str = "de", canton: str | None = None,
                 top_n: int = 10) -> list[dict]:
    conn = _get_conn()
    tsquery = " & ".join(f"'{t}'" for t in query.split() if t.strip())
    if not tsquery:
        return []
    results = []
    if not canton:
        # Search federal
        rows = conn.execute(
            """SELECT af.sr_number, af.article_num, af.heading, left(af.text, 500),
                      ts_rank_cd(af.fts, to_tsquery('simple', %s)) AS score,
                      lf.abbr_de, lf.title_de
               FROM articles_federal af
               LEFT JOIN laws_federal lf ON lf.sr_number = af.sr_number
               WHERE af.fts @@ to_tsquery('simple', %s) AND af.lang = %s
               ORDER BY score DESC LIMIT %s""",
            (tsquery, tsquery, language, top_n),
        ).fetchall()
        for r in rows:
            results.append({"type": "federal", "sr_number": r[0], "article": r[1],
                            "heading": r[2], "snippet": r[3], "score": float(r[4]),
                            "law_abbr": r[5], "law_title": r[6]})
    # Search cantonal
    cant_params = [tsquery, tsquery, language]
    cant_where = ""
    if canton:
        cant_where = "AND ac.canton = %s"; cant_params.append(canton)
    cant_params.append(top_n)
    rows = conn.execute(
        f"""SELECT ac.lexfind_id, ac.canton, ac.article_num, ac.heading,
                   left(ac.text, 500),
                   ts_rank_cd(ac.fts, to_tsquery('simple', %s)) AS score,
                   lc.title, lc.sr_number
            FROM articles_cantonal ac
            LEFT JOIN laws_cantonal lc ON lc.lexfind_id = ac.lexfind_id AND lc.language = ac.language
            WHERE ac.fts @@ to_tsquery('simple', %s) AND ac.language = %s {cant_where}
            ORDER BY score DESC LIMIT %s""",
        cant_params,
    ).fetchall()
    for r in rows:
        results.append({"type": "cantonal", "canton": r[1], "article": r[2],
                        "heading": r[3], "snippet": r[4], "score": float(r[5]),
                        "law_title": r[6], "sr_number": r[7]})
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_n]


# ---------------------------------------------------------------------------
# 18. get_decision_enrichment
# ---------------------------------------------------------------------------

def _get_enrichment(raw_id: str) -> dict | None:
    decision_id = _resolve_id(raw_id)
    conn = _get_conn()
    row = conn.execute(
        """SELECT decision_id, court, procedural_stage, outcome, subject_matter,
                  principle_questions, obiter_dicta, doctrine_discussion,
                  language_detected, status
           FROM decision_enrichment WHERE decision_id = %s""",
        (decision_id,),
    ).fetchone()
    if not row:
        return None
    cols = ["decision_id", "court", "procedural_stage", "outcome", "subject_matter",
            "principle_questions", "obiter_dicta", "doctrine_discussion",
            "language_detected", "status"]
    d = dict(zip(cols, row))
    auth = conn.execute(
        """SELECT court_level, atf_published, authority_score,
                  pagerank_temporal, validity_status,
                  n_overruled_by, n_criticized_by, n_confirmed_by, n_cited_by
           FROM decision_authority WHERE decision_id = %s""",
        (decision_id,),
    ).fetchone()
    if auth:
        d["authority"] = {
            "court_level": auth[0], "atf_published": auth[1],
            "authority_score": auth[2], "pagerank_temporal": auth[3],
            "validity_status": auth[4],
            "n_overruled_by": auth[5], "n_criticized_by": auth[6],
            "n_confirmed_by": auth[7], "n_cited_by": auth[8],
        }
    laws = conn.execute(
        """SELECT clc.law_abbr, clc.article_num, clc.paragraph, clc.normalized, clc.sr_number
           FROM chunk_law_citations clc JOIN chunks c ON c.id = clc.chunk_id
           WHERE c.decision_id = %s ORDER BY clc.law_abbr, clc.article_num""",
        (decision_id,),
    ).fetchall()
    d["law_citations"] = [{"law": r[0], "article": r[1], "paragraph": r[2],
                            "normalized": r[3], "sr_number": r[4]} for r in laws]
    return d


# ---------------------------------------------------------------------------
# 19. find_related
# ---------------------------------------------------------------------------

def _get_article_history(sr_number: str, article: str, language: str = "de", as_of: str | None = None) -> dict:
    """Get version history of a law article, or text at a specific date."""
    conn = _get_conn()
    if as_of:
        # Return the version in force at that date
        row = conn.execute("""
            SELECT heading, text, footnote, valid_from, valid_to, status, change_type, change_ref, diff_summary
            FROM article_versions
            WHERE sr_number = %s AND article_num = %s AND lang = %s
              AND valid_from <= %s AND (valid_to IS NULL OR valid_to > %s)
            ORDER BY valid_from DESC LIMIT 1
        """, (sr_number, article, language, as_of, as_of)).fetchone()
        if not row:
            return {"error": f"No version found for art. {article} SR {sr_number} ({language}) as of {as_of}"}
        return {
            "sr_number": sr_number, "article": article, "language": language, "as_of": as_of,
            "heading": row[0], "text": row[1], "footnote": row[2],
            "valid_from": str(row[3]), "valid_to": str(row[4]) if row[4] else None,
            "status": row[5], "change_type": row[6],
        }
    # Full history
    rows = conn.execute("""
        SELECT heading, text, valid_from, valid_to, status, change_type, change_ref, diff_summary
        FROM article_versions
        WHERE sr_number = %s AND article_num = %s AND lang = %s
        ORDER BY valid_from ASC
    """, (sr_number, article, language)).fetchall()
    if not rows:
        return {"error": f"No history found for art. {article} SR {sr_number} ({language}). Run update_laws.py first."}
    versions = [
        {"heading": r[0], "text": r[1], "valid_from": str(r[2]),
         "valid_to": str(r[3]) if r[3] else None,
         "status": r[4], "change_type": r[5], "change_ref": r[6], "diff_summary": r[7]}
        for r in rows
    ]
    current = next((v for v in versions if v["status"] == "in_force"), versions[-1])
    return {
        "sr_number": sr_number, "article": article, "language": language,
        "current_text": current["text"],
        "current_heading": current["heading"],
        "total_versions": len(versions),
        "versions": versions,
    }


def _translate_decision(raw_id: str, target_lang: str) -> dict:
    """Translate a decision, using cache if available."""
    decision_id = _resolve_id(raw_id)
    conn = _get_conn()
    from search_stack.parag.translator import translate_decision, get_cached_translation
    # Check cache first (fast path)
    cached = get_cached_translation(conn, decision_id, target_lang)
    if cached:
        return cached
    # Full translation (slow path — 30-60s)
    return translate_decision(conn, decision_id, target_lang)


def _get_authority(court_code: str | None = None, canton: str | None = None) -> list[dict]:
    """Lookup authority by court_code or canton."""
    conn = _get_conn()
    if court_code:
        # Try exact match first, then partial
        row = conn.execute(
            "SELECT * FROM authorities WHERE court_code = %s", (court_code,)
        ).fetchone()
        if row:
            cols = [d.name for d in conn.execute("SELECT * FROM authorities LIMIT 0").description]
            return [dict(zip(cols, row))]
        # Partial match on name or court_code
        rows = conn.execute(
            """SELECT * FROM authorities
               WHERE court_code ILIKE %s OR name_fr ILIKE %s OR name_de ILIKE %s
               LIMIT 10""",
            (f"%{court_code}%", f"%{court_code}%", f"%{court_code}%"),
        ).fetchall()
    elif canton:
        rows = conn.execute(
            "SELECT * FROM authorities WHERE canton = %s ORDER BY level DESC, name_fr",
            (canton.upper(),),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM authorities ORDER BY level DESC, canton, name_fr LIMIT 20"
        ).fetchall()
    if not rows:
        return {"message": "No authorities found. The directory is being populated — data will be added progressively via scraping of official court websites."}
    cols = [d.name for d in conn.execute("SELECT * FROM authorities LIMIT 0").description]
    return [dict(zip(cols, r)) for r in rows]


def _search_judges(name: str | None = None, court_code: str | None = None,
                   function: str | None = None, canton: str | None = None,
                   active_only: bool = True) -> list[dict]:
    """Search judges roster."""
    conn = _get_conn()
    clauses = []
    params = []
    if name:
        clauses.append("(j.last_name ILIKE %s OR j.first_name ILIKE %s)")
        params.extend([f"%{name}%", f"%{name}%"])
    if court_code:
        clauses.append("a.court_code = %s")
        params.append(court_code)
    if function:
        clauses.append("j.function ILIKE %s")
        params.append(f"%{function}%")
    if canton:
        clauses.append("a.canton = %s")
        params.append(canton.upper())
    if active_only:
        clauses.append("j.end_date IS NULL")
    where = " AND ".join(clauses) if clauses else "1=1"
    rows = conn.execute(f"""
        SELECT j.last_name, j.first_name, j.title, j.function, j.chamber,
               j.language, j.start_date, j.end_date, j.party,
               a.court_code, a.name_fr, a.canton, a.level
        FROM judges j
        JOIN authorities a ON a.id = j.authority_id
        WHERE {where}
        ORDER BY a.level DESC, j.last_name
        LIMIT 50
    """, params).fetchall()
    if not rows:
        return {"message": "No judges found. The roster is being populated — data will be added progressively."}
    return [
        {"last_name": r[0], "first_name": r[1], "title": r[2], "function": r[3],
         "chamber": r[4], "language": r[5], "start_date": str(r[6]) if r[6] else None,
         "end_date": str(r[7]) if r[7] else None, "party": r[8],
         "court_code": r[9], "court_name": r[10], "canton": r[11], "court_level": r[12]}
        for r in rows
    ]


def _get_legal_context(query: str, top_n: int = 2) -> list[dict]:
    """Search doctrine_nodes for relevant legal context sheets."""
    conn = _get_conn()
    query_vec = _embed_query(query)
    vec_text = _vec_to_text(query_vec)
    # Hybrid: vector similarity + FTS
    rows = conn.execute(
        """SELECT id, title_fr, title_de, title_it, content, articles,
                  1 - (embedding <=> %s::vector) AS similarity,
                  ts_rank_cd(to_tsvector('simple', coalesce(title_fr,'') || ' ' || content),
                             plainto_tsquery('simple', %s)) AS fts_score
           FROM doctrine_nodes
           WHERE embedding IS NOT NULL
           ORDER BY (0.7 * (1 - (embedding <=> %s::vector)) + 0.3 * ts_rank_cd(
               to_tsvector('simple', coalesce(title_fr,'') || ' ' || content),
               plainto_tsquery('simple', %s))) DESC
           LIMIT %s""",
        (vec_text, query, vec_text, query, top_n),
    ).fetchall()
    return [
        {"id": r[0], "title_fr": r[1], "title_de": r[2], "title_it": r[3],
         "content": r[4], "articles": r[5],
         "similarity": round(r[6], 4), "fts_score": round(r[7], 4)}
        for r in rows
    ]


def _find_related(raw_id: str, top_n: int = 5) -> list[dict]:
    decision_id = _resolve_id(raw_id)
    conn = _get_conn()
    row = conn.execute(
        """SELECT ce.embedding FROM chunk_embeddings ce
           JOIN chunks c ON c.id = ce.chunk_id
           WHERE c.decision_id = %s LIMIT 1""",
        (decision_id,),
    ).fetchone()
    if not row:
        return []
    vec_text = str(row[0])
    rows = conn.execute(
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
    seen: set = set()
    out = []
    for r in sorted(rows, key=lambda x: x[3], reverse=True):
        if r[0] in seen:
            continue
        seen.add(r[0])
        out.append({"decision_id": r[0], "court": r[1], "summary": r[2],
                     "similarity": round(r[3], 4),
                     "date": str(r[4]) if r[4] else None,
                     "docket": r[5], "title": r[6]})
        if len(out) >= top_n:
            break
    return out


# ---------------------------------------------------------------------------
# 20-22. Placeholders
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    Tool(name="search_decisions",
         description=(
             "Search 965,000+ Swiss court decisions using hybrid retrieval: full-text (BM25) "
             "+ semantic vector search (BGE-M3 embeddings) + authority reranking (court hierarchy, "
             "PageRank, validity status).\n\n"
             "IMPORTANT — TRILINGUAL SEARCH STRATEGY:\n"
             "Swiss law is trilingual (DE/FR/IT). The same legal concept has different terms:\n"
             "  - Verjährung / prescription / prescrizione\n"
             "  - Haftpflicht / responsabilité civile / responsabilità civile\n"
             "  - Kündigung / résiliation / disdetta\n"
             "For thorough research, ALWAYS search in all three languages, especially for the "
             "Federal Supreme Court (bge/bger) which publishes in the language of the case.\n"
             "Do NOT set the 'language' filter unless the user explicitly asks for one language — "
             "omitting it searches all languages simultaneously.\n\n"
             "SEARCH TIPS:\n"
             "- Use specific legal terms, not vague phrases. 'Art. 41 OR Schadenersatz' > 'dommage'\n"
             "- Combine statute references with concepts: 'Mietrecht Kündigung Art. 271 OR'\n"
             "- For leading cases, prefer find_leading_cases instead\n"
             "- Results include authority_score (0-1), court_level (1-5), and validity_status\n"
             "- Set court_level_min=4 to restrict to federal courts (TF/TAF/TPF)\n"
             "- Court codes: bge (published ATF), bger (unpublished TF), bvger (TAF), bstger (TPF)"
         ),
         inputSchema={"type": "object", "properties": {
             "query": {"type": "string", "description": (
                 "Search query. Use legal terminology in DE, FR, or IT. "
                 "Examples: 'Verjährung Forderung', 'prescription créance', "
                 "'résiliation bail commercial', 'Haftung Tierhalter Art. 56 OR'"
             )},
             "language": {"type": "string", "enum": ["de", "fr", "it"],
                          "description": "Filter by decision language. OMIT to search all three languages (recommended)."},
             "courts": {"type": "array", "items": {"type": "string"},
                        "description": "Filter by court codes. Examples: ['bge','bger'] for federal supreme, ['bvger'] for TAF. Omit for all courts."},
             "court_level_min": {"type": "integer", "minimum": 1, "maximum": 5,
                                 "description": "Minimum court level: 1=admin authorities, 2=first instance, 3=cantonal supreme, 4=federal specialised (TAF/TPF), 5=Federal Supreme Court (TF)"},
             "exclude_overruled": {"type": "boolean", "default": True,
                                   "description": "Exclude decisions with validity_status='overruled'. Default true."},
             "top_n": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50,
                       "description": "Number of results to return"},
         }, "required": ["query"]}),

    Tool(name="get_decision",
         description=(
             "Fetch a single Swiss court decision with full text, metadata, regeste, and "
             "SAC-enriched chunks (with AI-generated summaries per Erwägung).\n\n"
             "Accepts multiple ID formats:\n"
             "- decision_id: 'bge_BGE_145_III_345', 'bger_4A_295_2020'\n"
             "- BGE reference: 'BGE 145 III 345', '145 III 345'\n"
             "- Docket number: '4A_295/2020', '6B_1/2025'\n\n"
             "The response includes chunks split by Erwägung (considerant), each with an "
             "optional summary. Use get_erwaegung for the full text of a specific section."
         ),
         inputSchema={"type": "object", "properties": {
             "decision_id": {"type": "string", "description": (
                 "Any reference format: decision_id, BGE ref ('BGE 145 III 345'), "
                 "or docket number ('4A_295/2020')"
             )},
         }, "required": ["decision_id"]}),

    Tool(name="list_courts",
         description=(
             "List all ~100 Swiss courts in the database with decision counts, date ranges, "
             "and language coverage. Use this to discover available courts and their codes.\n"
             "Key courts: bge (ATF published), bger (TF unpublished), bvger (TAF), "
             "bstger (TPF), bpatger (TFB). Cantonal courts follow the pattern: "
             "{canton}_{court_type} (e.g. zh_obergericht, ge_gerichte, vd_findinfo)."
         ),
         inputSchema={"type": "object", "properties": {}}),

    Tool(name="get_statistics",
         description="Database statistics: total decisions, chunks, embeddings, enrichments, citations. Optionally filter by court code, canton, or year.",
         inputSchema={"type": "object", "properties": {
             "court": {"type": "string", "description": "Filter by court code (e.g. 'bger')"},
             "canton": {"type": "string", "description": "Filter by canton code (e.g. 'ZH', 'GE')"},
             "year": {"type": "integer", "description": "Filter by decision year"},
         }}),

    Tool(name="find_citations",
         description=(
             "Show what a decision cites (outbound) and what cites it (inbound). "
             "Uses the reference graph with 8.9M citation edges.\n"
             "Inbound citations indicate influence: a decision cited by many later cases "
             "is more authoritative. Outbound citations show the legal foundation.\n"
             "Results include the citing court, date, and docket number."
         ),
         inputSchema={"type": "object", "properties": {
             "decision_id": {"type": "string", "description": "decision_id, BGE ref, or docket number"},
             "direction": {"type": "string", "enum": ["both", "outbound", "inbound"], "default": "both",
                           "description": "'both' (default), 'outbound' (what this decision cites), 'inbound' (what cites this decision)"},
             "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
         }, "required": ["decision_id"]}),

    Tool(name="find_appeal_chain",
         description=(
             "Trace the appeal chain (Instanzenzug / voie de recours) for a decision. "
             "Reconstructs the path from first instance through cantonal courts to the "
             "Federal Supreme Court. Follows citation links across court levels."
         ),
         inputSchema={"type": "object", "properties": {
             "decision_id": {"type": "string"},
         }, "required": ["decision_id"]}),

    Tool(name="find_leading_cases",
         description=(
             "Find the most authoritative (leading) decisions for a topic or statute article. "
             "Ranked by PA-RAG authority score combining court hierarchy, citation PageRank, "
             "and validity status.\n\n"
             "Two modes:\n"
             "- By topic: semantic search + authority reranking (provide 'topic')\n"
             "- By statute: finds decisions citing a specific article (provide 'sr_number' + 'article')\n"
             "Both can be combined.\n\n"
             "TRILINGUAL: Provide the topic in all relevant languages for comprehensive results."
         ),
         inputSchema={"type": "object", "properties": {
             "topic": {"type": "string", "description": "Legal topic (e.g. 'Tierhalterhaftung', 'responsabilité du détenteur d'animal')"},
             "sr_number": {"type": "string", "description": "SR number of the law (e.g. '220' for OR, '210' for ZGB, '311.0' for StGB)"},
             "article": {"type": "string", "description": "Article number (e.g. '41', '8', '56')"},
             "top_n": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
         }}),

    Tool(name="analyze_legal_trend",
         description=(
             "Year-by-year decision counts showing how jurisprudence on a topic or statute "
             "has evolved over time. Useful to detect emerging legal issues or declining relevance."
         ),
         inputSchema={"type": "object", "properties": {
             "topic": {"type": "string", "description": "Search topic (FTS)"},
             "sr_number": {"type": "string", "description": "SR number of the law"},
             "article": {"type": "string", "description": "Article number"},
         }}),

    Tool(name="draft_mock_decision",
         description=(
             "Build a research-only mock decision outline from user facts. "
             "Searches for relevant Swiss case law, identifies applicable statutes, "
             "and structures an analysis following Swiss judicial reasoning patterns "
             "(Sachverhalt → Erwägungen → Dispositiv).\n\n"
             "NOT legal advice — for research and educational purposes only.\n"
             "The tool may ask clarification questions about missing facts."
         ),
         inputSchema={"type": "object", "properties": {
             "facts": {"type": "string", "description": "Detailed facts of the case to analyze"},
             "language": {"type": "string", "enum": ["de", "fr", "it"], "default": "de",
                          "description": "Output language for the analysis"},
             "top_n": {"type": "integer", "default": 5, "minimum": 1, "maximum": 10,
                       "description": "Number of relevant cases to find"},
         }, "required": ["facts"]}),

    Tool(name="get_case_brief",
         description=(
             "Structured case brief for any Swiss court decision. Returns:\n"
             "- Regeste (official headnote — the rule established)\n"
             "- Sachverhalt (facts summary from first chunks)\n"
             "- Key Erwägungen (reasoning excerpts with paragraph numbers)\n"
             "- Dispositiv (holding/ruling)\n"
             "- Applicable statutes cited in the decision\n"
             "- Citation authority (how many decisions cite this one)\n\n"
             "Accepts any reference: BGE ref, decision_id, or docket number."
         ),
         inputSchema={"type": "object", "properties": {
             "decision_id": {"type": "string", "description": "BGE ref ('BGE 133 III 121'), decision_id, or docket number"},
         }, "required": ["decision_id"]}),

    Tool(name="get_decision_structure",
         description=(
             "Structural outline of a decision: Sachverhalt, numbered Erwägungen "
             "(with summaries), and Dispositiv — extracted from SAC-enriched chunks. "
             "Use this to understand the decision's reasoning structure before reading "
             "specific sections with get_erwaegung."
         ),
         inputSchema={"type": "object", "properties": {
             "decision_id": {"type": "string"},
         }, "required": ["decision_id"]}),

    Tool(name="get_erwaegung",
         description=(
             "Fetch the FULL VERBATIM TEXT of a single numbered Erwägung (considerant). "
             "This is the citable unit in Swiss legal practice — lawyers cite "
             "'BGE 140 III 86 E. 2.3' to refer to a specific paragraph.\n\n"
             "The considerant_number can be top-level ('1', '2') or sub-level ('1.1', '2.3')."
         ),
         inputSchema={"type": "object", "properties": {
             "decision_id": {"type": "string", "description": "decision_id, BGE ref, or docket number"},
             "considerant_number": {"type": "string", "description": "Erwägung number: '3', '3.2', '5.2.1'. Leading 'E.' is stripped."},
         }, "required": ["decision_id", "considerant_number"]}),

    Tool(name="get_regeste",
         description=(
             "Get the official Regeste (headnote / Leitsatz) of a decision. The Regeste is "
             "the court's own formulation of the legal rule established. For BGEs, this is "
             "the canonical citation target. Available for ~54% of federal decisions (100% of BGE)."
         ),
         inputSchema={"type": "object", "properties": {
             "decision_id": {"type": "string", "description": "decision_id, BGE ref, or docket number"},
         }, "required": ["decision_id"]}),

    Tool(name="get_doctrine",
         description=(
             "Comprehensive doctrinal analysis for a legal concept or statute article:\n"
             "1. Current statute text (from Fedlex mirror)\n"
             "2. Leading cases ranked by authority\n"
             "3. Year-by-year jurisprudence timeline\n\n"
             "ALWAYS USE THIS for questions about the meaning, scope, or application of a "
             "Swiss law provision. Combines statute lookup + case search + authority ranking.\n\n"
             "TRILINGUAL: The concept should be provided in the user's language. "
             "The search covers all three languages internally."
         ),
         inputSchema={"type": "object", "properties": {
             "concept": {"type": "string", "description": (
                 "Legal concept or statute reference. Examples: 'Art. 41 OR', "
                 "'Tierhalterhaftung', 'responsabilité contractuelle', 'culpa in contrahendo'"
             )},
             "sr_number": {"type": "string", "description": "SR number for statute lookup (e.g. '220' for OR, '210' for ZGB)"},
             "article": {"type": "string", "description": "Article number (e.g. '41', '8')"},
             "language": {"type": "string", "enum": ["de", "fr", "it"], "default": "de"},
             "top_n": {"type": "integer", "default": 10, "description": "Number of leading cases to include"},
         }, "required": ["concept"]}),

    Tool(name="generate_exam_question",
         description=(
             "Generate a Swiss law practice exam question (Fallbearbeitung) from a real BGE. "
             "Returns a fact pattern (Sachverhalt) extracted from a real case + hidden analysis "
             "(applicable statutes, legal test, correct outcome).\n\n"
             "Workflow: present the fact pattern to the student, let them analyze, then reveal "
             "the hidden analysis for comparison. Call get_case_brief(source_decision_id) to "
             "study the full source case afterwards."
         ),
         inputSchema={"type": "object", "properties": {
             "topic": {"type": "string", "description": (
                 "Legal area or concept. Examples: 'Haftpflichtrecht', 'Art. 41 OR', "
                 "'Mietrecht', 'droit des contrats'"
             )},
             "language": {"type": "string", "enum": ["de", "fr", "it"], "default": "de",
                          "description": "Language for the generated question"},
         }, "required": ["topic"]}),

    Tool(name="get_law",
         description=(
             "AUTHORITATIVE LOOKUP for the current text of any Swiss law article — "
             "federal (5,509 laws from Fedlex) OR cantonal (15,722 laws from LexFind, "
             "all 26 cantons).\n\n"
             "USE THIS BEFORE relying on training data — Swiss statute text changes frequently "
             "and LLMs routinely hallucinate article content.\n\n"
             "Common SR numbers: 101=BV, 210=ZGB, 220=OR, 311.0=StGB, 272=ZPO, 312.0=StPO, "
             "281.1=SchKG/LP, 173.110=BGG/LTF.\n\n"
             "For cantonal laws, specify the canton code. Use search_laws to discover available laws."
         ),
         inputSchema={"type": "object", "properties": {
             "sr_number": {"type": "string", "description": "SR number (e.g. '210' for ZGB, '220' for OR, '101' for BV)"},
             "abbreviation": {"type": "string", "description": "Law abbreviation: BV, ZGB, OR, StGB, StPO, ZPO, BGG, SchKG, LP, etc."},
             "article": {"type": "string", "description": "Article number (e.g. '8', '41', '271a'). Omit to list all articles."},
             "language": {"type": "string", "enum": ["de", "fr", "it"], "default": "de",
                          "description": "Language for article text. Cantonal: match the canton's language (fr for GE/VD/NE/JU/FR, it for TI)."},
             "canton": {"type": "string", "description": "Two-letter canton code for cantonal laws (ZH, BE, GE, VD, etc.). Omit or 'CH' for federal."},
         }}),

    Tool(name="search_laws",
         description=(
             "Full-text search across all Swiss statute articles — both federal (Fedlex) "
             "and cantonal (LexFind, all 26 cantons). Returns ranked snippets with article "
             "number, heading, law title, and jurisdiction.\n\n"
             "Use this as the ENTRY POINT when you don't know which law applies. "
             "For a specific known article, use get_law instead."
         ),
         inputSchema={"type": "object", "properties": {
             "query": {"type": "string", "description": "Search query (FTS). Examples: 'Verjährung', 'prescription', 'Hundehaltung'"},
             "language": {"type": "string", "enum": ["de", "fr", "it"], "default": "de"},
             "canton": {"type": "string", "description": "Restrict to a canton (e.g. 'ZH'). Omit for all jurisdictions."},
             "top_n": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
         }, "required": ["query"]}),

    Tool(name="get_decision_enrichment",
         description=(
             "Get AI-extracted metadata for a decision (PA-RAG Phase 5 enrichment):\n"
             "- procedural_stage: recours, premiere_instance, appel, etc.\n"
             "- outcome: admission, rejet, irrecevabilite, etc.\n"
             "- principle_questions: ratio decidendi — the legal questions answered, with legal basis\n"
             "- obiter_dicta: statements not essential to the holding\n"
             "- doctrine_discussion: whether scholarly authors were weighed\n"
             "- authority: court_level, PageRank, validity_status, citation counts\n"
             "- law_citations: statutes cited in the decision's text\n\n"
             "Available for ~740,000 decisions (enrichment ongoing)."
         ),
         inputSchema={"type": "object", "properties": {
             "decision_id": {"type": "string", "description": "decision_id, BGE ref, or docket number"},
         }, "required": ["decision_id"]}),

    Tool(name="find_related",
         description=(
             "Find decisions semantically similar to a given decision, using vector cosine "
             "similarity on BGE-M3 chunk embeddings. Useful for finding parallel cases, "
             "analogous reasoning, or conflicting jurisprudence."
         ),
         inputSchema={"type": "object", "properties": {
             "decision_id": {"type": "string", "description": "decision_id, BGE ref, or docket number"},
             "top_n": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
         }, "required": ["decision_id"]}),

    Tool(name="get_legal_context",
         description=(
             "Get doctrinal context for a legal topic BEFORE searching case law. "
             "Returns 1-3 structured knowledge sheets covering: key legal framework, "
             "essential distinctions, central articles, recent developments, common pitfalls, "
             "and trilingual terminology.\n\n"
             "ALWAYS CALL THIS FIRST when researching a new legal question. It provides "
             "the conceptual framing needed to search effectively and avoid common mistakes "
             "(e.g., confusing bail d'habitation/commercial, résiliation ordinaire/extraordinaire, etc.).\n\n"
             "Covers 116 domains of Swiss law: obligations, family, succession, real property, "
             "criminal, procedure, enforcement, tax, social insurance, constitutional rights, etc."
         ),
         inputSchema={"type": "object", "properties": {
             "query": {"type": "string", "description": (
                 "Legal topic, concept, or statute reference. "
                 "Examples: 'résiliation bail', 'prescription créance', 'détention provisoire', "
                 "'Art. 41 OR', 'divorce entretien'"
             )},
             "top_n": {"type": "integer", "default": 2, "minimum": 1, "maximum": 5,
                       "description": "Number of context sheets to return (default 2)"},
         }, "required": ["query"]}),

    Tool(name="get_authority",
         description=(
             "Get contact information and details for a Swiss judicial authority (court, tribunal, "
             "regulatory body). Returns: full name (trilingual), address, phone, email, secure email "
             "(IncaMail/Privasphere), website, jurisdiction, chambers, and hierarchy level.\n\n"
             "Accepts court codes (e.g. 'bger', 'zh_obergericht', 'bvger') or search terms."
         ),
         inputSchema={"type": "object", "properties": {
             "court_code": {"type": "string", "description": (
                 "Court code (e.g. 'bger', 'zh_obergericht', 'bvger', 'ge_gerichte') "
                 "or partial name to search"
             )},
             "canton": {"type": "string", "description": "Filter by canton (e.g. 'ZH', 'GE')"},
         }}),

    Tool(name="search_judges",
         description=(
             "Search the roster of Swiss judges and court clerks. Find judges by name, "
             "court, function (président, juge, greffier), or chamber. Returns active judges "
             "with their authority, function, and tenure.\n\n"
             "Data sources: official court websites, annuaire.admin.ch, extracted from decisions."
         ),
         inputSchema={"type": "object", "properties": {
             "name": {"type": "string", "description": "Last name (partial match supported)"},
             "court_code": {"type": "string", "description": "Filter by court code"},
             "function": {"type": "string", "description": "Filter: 'président', 'juge', 'greffier', 'suppléant'"},
             "canton": {"type": "string", "description": "Filter by canton"},
             "active_only": {"type": "boolean", "default": True, "description": "Only show currently active judges"},
         }}),
    Tool(name="translate_decision",
         description=(
             "Translate a Swiss court decision to another language (FR/DE/IT/EN). "
             "Uses a cache: if the translation already exists, returns it instantly. "
             "Otherwise, translates the full text + regeste using Gemini 2.5 Flash "
             "with professional legal terminology.\n\n"
             "Swiss law abbreviations are automatically converted to the target language "
             "(OR→CO, ZGB→CC, StGB→CP, SchKG→LP, BGG→LTF, etc.).\n\n"
             "First translation takes 30-60 seconds. Subsequent requests for the same "
             "decision+language are instant (cached)."
         ),
         inputSchema={"type": "object", "properties": {
             "decision_id": {"type": "string", "description": "decision_id, BGE ref, or docket number"},
             "target_lang": {"type": "string", "enum": ["fr", "de", "it", "en"],
                             "description": "Target language for translation"},
         }, "required": ["decision_id", "target_lang"]}),

    Tool(name="get_article_history",
         description=(
             "Get the complete version history of a Swiss law article. Returns ALL versions "
             "chronologically: initial text, each modification, and current version — with dates, "
             "change descriptions, and diff summaries.\n\n"
             "Use this to understand how a provision has evolved over time, identify when a "
             "specific change was introduced, or find the text as it was at a specific date.\n\n"
             "With `as_of`: returns the text as it was in force on that date.\n"
             "Without `as_of`: returns the full chronological history."
         ),
         inputSchema={"type": "object", "properties": {
             "sr_number": {"type": "string", "description": "SR number (e.g. '220' for OR, '210' for ZGB)"},
             "article": {"type": "string", "description": "Article number (e.g. '41', '8')"},
             "language": {"type": "string", "enum": ["de", "fr", "it"], "default": "de"},
             "as_of": {"type": "string", "description": "ISO date (e.g. '2020-01-01') — returns the version in force at that date"},
         }, "required": ["sr_number", "article"]}),
]

# Tools pending migration — not exposed until data is in Postgres:
# get_commentary, search_commentaries (OnlineKommentar.ch)
# get_materialien, search_materialien (Botschaft / legislative history)


# ---------------------------------------------------------------------------
# Rate limiter (per-session, 20 req/min)
# ---------------------------------------------------------------------------

import collections
import time as _time

_RATE_LIMIT = int(os.environ.get("MCP_RATE_LIMIT", "60"))
_RATE_WINDOW = 60
_rate_buckets: dict[str, collections.deque] = {}


def _check_rate_limit(session_id: str = "global") -> bool:
    now = _time.monotonic()
    bucket = _rate_buckets.setdefault(session_id, collections.deque())
    while bucket and bucket[0] < now - _RATE_WINDOW:
        bucket.popleft()
    if len(bucket) >= _RATE_LIMIT:
        return False
    bucket.append(now)
    return True


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

app = Server("pa-rag")


@app.list_tools()
async def list_tools():
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    if not _check_rate_limit():
        return _jsonout({"error": f"Rate limit exceeded ({_RATE_LIMIT} requests/minute). Please slow down."})
    try:
        result = _dispatch(name, arguments)
        return _jsonout(result)
    except Exception as e:
        log.exception("Tool %s failed", name)
        return _jsonout({"error": str(e)})


def _dispatch(name: str, args: dict):
    if name == "search_decisions":
        return _search_decisions(
            query=args["query"], language=args.get("language"),
            courts=args.get("courts"), court_level_min=args.get("court_level_min"),
            exclude_overruled=args.get("exclude_overruled", True),
            top_n=args.get("top_n", 10))
    if name == "get_decision":
        r = _get_decision(args["decision_id"])
        return r if r else _not_found(args["decision_id"])
    if name == "list_courts":
        return _list_courts()
    if name == "get_statistics":
        return _get_statistics(court=args.get("court"), canton=args.get("canton"),
                               year=args.get("year"))
    if name == "find_citations":
        return _find_citations(args["decision_id"],
                               direction=args.get("direction", "both"),
                               limit=args.get("limit", 20))
    if name == "find_appeal_chain":
        return _find_appeal_chain(args["decision_id"])
    if name == "find_leading_cases":
        return _find_leading_cases(topic=args.get("topic"), sr_number=args.get("sr_number"),
                                   article=args.get("article"), top_n=args.get("top_n", 10))
    if name == "analyze_legal_trend":
        return _analyze_legal_trend(topic=args.get("topic"), sr_number=args.get("sr_number"),
                                    article=args.get("article"))
    if name == "draft_mock_decision":
        return _draft_mock_decision(facts=args["facts"], language=args.get("language", "de"),
                                    top_n=args.get("top_n", 5))
    if name == "get_case_brief":
        r = _get_case_brief(args["decision_id"])
        return r if r else _not_found(args["decision_id"])
    if name == "get_decision_structure":
        r = _get_decision_structure(args["decision_id"])
        return r if r else _not_found(args["decision_id"])
    if name == "get_erwaegung":
        r = _get_erwaegung(args["decision_id"], args["considerant_number"])
        return r if r else {"error": f"Erwägung '{args['considerant_number']}' not found in '{args['decision_id']}'"}
    if name == "get_regeste":
        r = _get_regeste(args["decision_id"])
        return r if r else _not_found(args["decision_id"])
    if name == "get_doctrine":
        return _get_doctrine(concept=args["concept"], sr_number=args.get("sr_number"),
                             article=args.get("article"), language=args.get("language", "de"),
                             top_n=args.get("top_n", 10))
    if name == "generate_exam_question":
        return _generate_exam_question(topic=args["topic"], language=args.get("language", "de"))
    if name == "get_law":
        return _get_law(sr_number=args.get("sr_number"), abbreviation=args.get("abbreviation"),
                        article=args.get("article"), language=args.get("language", "de"),
                        canton=args.get("canton"))
    if name == "search_laws":
        return _search_laws(query=args["query"], language=args.get("language", "de"),
                            canton=args.get("canton"), top_n=args.get("top_n", 10))
    if name == "get_decision_enrichment":
        r = _get_enrichment(args["decision_id"])
        return r if r else {"error": f"No enrichment for '{args['decision_id']}'"}
    if name == "find_related":
        return _find_related(args["decision_id"], top_n=args.get("top_n", 5))
    if name == "get_legal_context":
        return _get_legal_context(query=args["query"], top_n=args.get("top_n", 2))
    if name == "get_authority":
        return _get_authority(court_code=args.get("court_code"), canton=args.get("canton"))
    if name == "search_judges":
        return _search_judges(name=args.get("name"), court_code=args.get("court_code"),
                              function=args.get("function"), canton=args.get("canton"),
                              active_only=args.get("active_only", True))
    if name == "translate_decision":
        return _translate_decision(args["decision_id"], args["target_lang"])
    if name == "get_article_history":
        return _get_article_history(sr_number=args["sr_number"], article=args["article"],
                                    language=args.get("language", "de"), as_of=args.get("as_of"))
    return {"error": f"Unknown tool: {name}"}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    mode = os.environ.get("MCP_TRANSPORT", "stdio")

    if mode == "sse":
        from starlette.applications import Starlette
        from starlette.routing import Route, Mount
        from mcp.server.sse import SseServerTransport
        import uvicorn

        sse = SseServerTransport("/messages/")

        async def handle_sse(request):
            async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
                await app.run(streams[0], streams[1], app.create_initialization_options())

        starlette_app = Starlette(routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ])

        port = int(os.environ.get("MCP_PORT", "8001"))
        log.info("PA-RAG MCP Server starting (SSE on port %d) — %d tools", port, len(TOOLS))
        uvicorn.run(starlette_app, host="0.0.0.0", port=port, log_level="info")
    else:
        import asyncio

        async def run_stdio():
            log.info("PA-RAG MCP Server starting (stdio) — %d tools", len(TOOLS))
            async with stdio_server() as (read, write):
                await app.run(read, write, app.create_initialization_options())

        asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
