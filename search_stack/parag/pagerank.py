"""Time-decayed PageRank over the decision citation graph.

Pilier 2 of PA-RAG authority scoring (Harvard Law paper, § 2.2):
a decision's importance is the PageRank of its in-degree in a graph
where the citation's weight decays exponentially with the citing
decision's age.

    w(A → B) = exp(-λ · (now - date(A)) / 365)

with λ ≈ 0.10 (half-life ~7 years). Calibrated later by grid search on
the Phase 9 benchmark.

The resulting `pagerank_temporal` is stored in decision_authority and
combined with court_level + validity_status at retrieval time.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime
from pathlib import Path


DEFAULT_LAMBDA = 0.10  # half-life ≈ 7 years


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _load_edges(
    graph_db: Path, source_db: Path,
) -> tuple[list[tuple[str, str, float]], set[str]]:
    """Read decision→decision edges from reference_graph.db, join with
    decision dates from decisions.db to compute time weights."""
    ref = sqlite3.connect(f"file:{graph_db}?mode=ro", uri=True)
    ref.row_factory = sqlite3.Row

    src = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row

    # Gather citing-decision dates in one pass to avoid per-row queries
    dates: dict[str, date] = {}
    for r in src.execute("SELECT decision_id, decision_date FROM decisions"):
        d = _parse_date(r["decision_date"])
        if d:
            dates[r["decision_id"]] = d
    src.close()

    today = date.today()
    edges: list[tuple[str, str, float]] = []
    nodes: set[str] = set()

    cur = ref.execute(
        "SELECT source_decision_id, target_decision_id FROM decision_citations"
    )
    for row in cur:
        a, b = row["source_decision_id"], row["target_decision_id"]
        if not a or not b or a == b:
            continue
        da = dates.get(a)
        age_years = (today - da).days / 365.25 if da else 7.0  # default 7y if unknown
        w = math.exp(-DEFAULT_LAMBDA * age_years)
        edges.append((a, b, w))
        nodes.add(a)
        nodes.add(b)
    ref.close()
    return edges, nodes


def compute_pagerank(
    edges: list[tuple[str, str, float]],
    nodes: set[str],
    *,
    alpha: float = 0.85,
    max_iter: int = 60,
    tol: float = 1e-6,
) -> dict[str, float]:
    """Weighted PageRank (power iteration). Standalone implementation to
    avoid the networkx dependency — good enough for ~8M edges in ~2 min
    on our laptop.
    """
    # Build out-adjacency with row-normalisation
    out_weight: dict[str, float] = {n: 0.0 for n in nodes}
    for a, _, w in edges:
        out_weight[a] = out_weight.get(a, 0.0) + w

    # Split edges into (a, b, normalized_weight)
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
        # Dangling nodes distribute their score uniformly
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
    parag_conn: sqlite3.Connection,
    *,
    graph_db: Path | None = None,
    source_db: Path | None = None,
    lambda_decay: float = DEFAULT_LAMBDA,
) -> dict:
    """Compute + persist pagerank_temporal for every decision in the graph."""
    graph_db = graph_db or (Path.home() / ".swiss-caselaw" / "reference_graph.db")
    source_db = source_db or (Path.home() / ".swiss-caselaw" / "decisions.db")

    if not graph_db.exists() or graph_db.stat().st_size == 0:
        return {"status": "no-graph", "nodes": 0, "edges": 0}

    edges, nodes = _load_edges(graph_db, source_db)
    if not edges:
        return {"status": "empty-graph", "nodes": 0, "edges": 0}

    pr = compute_pagerank(edges, nodes)

    cur = parag_conn.cursor()
    for node, score in pr.items():
        cur.execute("""
            INSERT INTO decision_authority (decision_id, pagerank_temporal, computed_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(decision_id) DO UPDATE SET
                pagerank_temporal = excluded.pagerank_temporal,
                computed_at       = datetime('now')
        """, (node, score))
    parag_conn.commit()
    return {"status": "ok", "nodes": len(nodes), "edges": len(edges)}
