"""Time-decayed PageRank over the decision citation graph.

Pilier 2 of PA-RAG authority scoring (Harvard Law paper, § 2.2):
a decision's importance is the PageRank of its in-degree in a graph
where the citation's weight decays exponentially with the citing
decision's age.

    w(A → B) = exp(-λ · (now - date(A)) / 365)

with λ ≈ 0.10 (half-life ~7 years).
Now reads edges and dates from Postgres (same DB).
"""

from __future__ import annotations

import math
from datetime import date, datetime

import psycopg


DEFAULT_LAMBDA = 0.10


def _parse_date(d) -> date | None:
    if isinstance(d, date):
        return d
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, str) and len(d) >= 10:
        try:
            return datetime.strptime(d[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _load_edges(
    conn: psycopg.Connection,
) -> tuple[list[tuple[str, str, float]], set[str]]:
    """Read decision→decision edges from decision_citations, join with
    decisions.decision_date to compute time weights."""
    today = date.today()

    with conn.cursor() as cur:
        cur.execute("""
            SELECT dc.source_decision_id, dc.target_decision_id, d.decision_date
            FROM decision_citations dc
            LEFT JOIN decisions d ON d.decision_id = dc.source_decision_id
        """)

        edges: list[tuple[str, str, float]] = []
        nodes: set[str] = set()
        for a, b, dt in cur:
            if not a or not b or a == b:
                continue
            da = _parse_date(dt)
            age_years = (today - da).days / 365.25 if da else 7.0
            w = math.exp(-DEFAULT_LAMBDA * age_years)
            edges.append((a, b, w))
            nodes.add(a)
            nodes.add(b)
    return edges, nodes


def compute_pagerank(
    edges: list[tuple[str, str, float]],
    nodes: set[str],
    *,
    alpha: float = 0.85,
    max_iter: int = 60,
    tol: float = 1e-6,
) -> dict[str, float]:
    out_weight: dict[str, float] = {n: 0.0 for n in nodes}
    for a, _, w in edges:
        out_weight[a] = out_weight.get(a, 0.0) + w

    norm_edges: list[tuple[str, str, float]] = []
    for a, b, w in edges:
        total = out_weight.get(a, 0.0)
        if total > 0:
            norm_edges.append((a, b, w / total))

    n = len(nodes)
    r = {node: 1.0 / n for node in nodes}
    teleport = (1.0 - alpha) / n
    dangling_nodes = [node for node in nodes if out_weight.get(node, 0.0) == 0.0]

    for it in range(max_iter):
        dangling_sum = sum(r[d] for d in dangling_nodes)
        base = teleport + alpha * dangling_sum / n
        new_r = {node: base for node in nodes}
        for a, b, nw in norm_edges:
            new_r[b] = new_r.get(b, base) + alpha * nw * r[a]
        diff = sum(abs(new_r[k] - r[k]) for k in nodes)
        r = new_r
        if diff < tol:
            break
    return r


def populate_pagerank(
    conn: psycopg.Connection,
    *,
    lambda_decay: float = DEFAULT_LAMBDA,
) -> dict:
    """Compute + persist pagerank_temporal for every decision in the graph."""
    edges, nodes = _load_edges(conn)
    if not edges:
        return {"status": "empty-graph", "nodes": 0, "edges": 0}

    pr = compute_pagerank(edges, nodes)

    with conn.cursor() as cur:
        for node, score in pr.items():
            cur.execute("""
                INSERT INTO decision_authority (decision_id, pagerank_temporal, computed_at)
                VALUES (%s, %s, now())
                ON CONFLICT(decision_id) DO UPDATE SET
                    pagerank_temporal = EXCLUDED.pagerank_temporal,
                    computed_at       = now()
            """, (node, score))
    conn.commit()
    return {"status": "ok", "nodes": len(nodes), "edges": len(edges)}
