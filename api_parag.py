"""PA-RAG REST API — FastAPI over Postgres+pgvector.

Endpoints:
    GET  /search?q=...&language=de&top_n=10   — Hybrid search (FTS + vector + authority)
    GET  /decision/{id}                        — Full decision + chunks
    GET  /decision/{id}/enrichment             — Phase 5 metadata
    GET  /decision/{id}/citations              — Citation graph
    GET  /decision/{id}/related                — Semantically similar decisions
    GET  /stats                                — Pipeline statistics
    GET  /health                               — Health check

Auth: Bearer token via X-API-Key header (configured in .env as API_KEY).
HTTPS: handled by Caddy reverse proxy in front.

Usage:
    PYTHONPATH=/srv/caselaw .venv/bin/uvicorn api_parag:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI, Query, HTTPException, Depends, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader

sys.path.insert(0, str(Path(__file__).resolve().parent))

from search_stack.parag.mcp_integration import (
    is_available,
    get_authority_batch,
    get_enrichment,
    get_chunks_for_decision,
    get_law_citations,
    hybrid_search,
    find_related_decisions,
    get_pg_statistics,
    _get_pg,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("api-parag")

# ---------------------------------------------------------------------------
# App + Auth
# ---------------------------------------------------------------------------

app = FastAPI(
    title="PA-RAG Swiss Case Law API",
    description="Hybrid retrieval API for Swiss court decisions with authority reranking.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _check_auth(api_key: str = Security(_api_key_header)):
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    pg = _get_pg()
    if not pg:
        raise HTTPException(status_code=503, detail="Auth service unavailable")
    row = pg.execute(
        "UPDATE api_keys SET last_used = now(), usage_count = usage_count + 1 "
        "WHERE key_id = %s AND is_active RETURNING name, scopes",
        (api_key,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid or deactivated API key")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    pg_ok = is_available()
    return {"status": "ok" if pg_ok else "degraded", "postgres": pg_ok}


@app.get("/stats", dependencies=[Depends(_check_auth)])
def stats():
    s = get_pg_statistics()
    if s is None:
        raise HTTPException(503, "Postgres unavailable")
    return s


@app.get("/search", dependencies=[Depends(_check_auth)])
def search(
    q: str = Query(..., description="Search query (DE/FR/IT)"),
    language: str | None = Query(None, description="Filter: de, fr, it"),
    courts: str | None = Query(None, description="Comma-separated court codes"),
    court_level_min: int | None = Query(None, ge=1, le=5, description="Min court level (1-5)"),
    exclude_overruled: bool = Query(True),
    top_n: int = Query(10, ge=1, le=50),
):
    court_list = [c.strip() for c in courts.split(",")] if courts else None
    results = hybrid_search(
        query=q, language=language, courts=court_list,
        court_level_min=court_level_min,
        exclude_overruled=exclude_overruled, top_n=top_n,
    )
    if results is None:
        raise HTTPException(503, "Search unavailable")
    return {"query": q, "count": len(results), "results": results}


@app.get("/decision/{decision_id}", dependencies=[Depends(_check_auth)])
def decision(decision_id: str):
    pg = _get_pg()
    if not pg:
        raise HTTPException(503, "Postgres unavailable")
    row = pg.execute(
        """SELECT decision_id, court, canton, chamber, docket_number,
                  decision_date, language, title, legal_area, regeste,
                  full_text, source_url
           FROM decisions WHERE decision_id = %s""",
        (decision_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, f"Decision {decision_id} not found")
    cols = ["decision_id", "court", "canton", "chamber", "docket_number",
            "decision_date", "language", "title", "legal_area", "regeste",
            "full_text", "source_url"]
    d = dict(zip(cols, row))
    d["decision_date"] = str(d["decision_date"]) if d["decision_date"] else None
    d["chunks"] = get_chunks_for_decision(decision_id)
    auth = get_authority_batch([decision_id])
    d["authority"] = auth.get(decision_id)
    return d


@app.get("/decision/{decision_id}/enrichment", dependencies=[Depends(_check_auth)])
def enrichment(decision_id: str):
    e = get_enrichment(decision_id)
    if e is None:
        raise HTTPException(404, f"No enrichment for {decision_id}")
    e["law_citations"] = get_law_citations(decision_id)
    auth = get_authority_batch([decision_id])
    e["authority"] = auth.get(decision_id)
    return e


@app.get("/decision/{decision_id}/citations", dependencies=[Depends(_check_auth)])
def citations(
    decision_id: str,
    direction: str = Query("both", description="both, inbound, outbound"),
    limit: int = Query(20, ge=1, le=100),
):
    pg = _get_pg()
    if not pg:
        raise HTTPException(503, "Postgres unavailable")
    result = {"decision_id": decision_id}
    if direction in ("outbound", "both"):
        rows = pg.execute(
            """SELECT dc.target_decision_id, dc.raw_text, dc.confidence_score,
                      d.court, d.decision_date, d.docket_number
               FROM decision_citations dc
               LEFT JOIN decisions d ON d.decision_id = dc.target_decision_id
               WHERE dc.source_decision_id = %s
               ORDER BY dc.confidence_score DESC NULLS LAST LIMIT %s""",
            (decision_id, limit),
        ).fetchall()
        result["cites"] = [
            {"decision_id": r[0], "raw_text": r[1], "confidence": r[2],
             "court": r[3], "date": str(r[4]) if r[4] else None, "docket": r[5]}
            for r in rows
        ]
    if direction in ("inbound", "both"):
        rows = pg.execute(
            """SELECT dc.source_decision_id, dc.raw_text, dc.confidence_score,
                      d.court, d.decision_date, d.docket_number
               FROM decision_citations dc
               LEFT JOIN decisions d ON d.decision_id = dc.source_decision_id
               WHERE dc.target_decision_id = %s
               ORDER BY d.decision_date DESC NULLS LAST LIMIT %s""",
            (decision_id, limit),
        ).fetchall()
        result["cited_by"] = [
            {"decision_id": r[0], "raw_text": r[1], "confidence": r[2],
             "court": r[3], "date": str(r[4]) if r[4] else None, "docket": r[5]}
            for r in rows
        ]
    return result


@app.get("/decision/{decision_id}/related", dependencies=[Depends(_check_auth)])
def related(decision_id: str, top_n: int = Query(5, ge=1, le=20)):
    results = find_related_decisions(decision_id, top_n=top_n)
    if results is None:
        raise HTTPException(503, "Vector search unavailable")
    return {"decision_id": decision_id, "related": results}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
