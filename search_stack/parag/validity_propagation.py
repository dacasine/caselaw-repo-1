"""Propagate validity_status from Phase 5 prior_case_treatment.

Every decision's Phase 5 enrichment stores a `prior_case_treatment`
array describing how THIS decision treats earlier arrêts (confirms,
overrules, criticizes, distinguishes, develops, neutral).

This module inverts that graph to derive the target decisions' status:
    B overruled by A, A overruled by C → B is overruled (by A).
    (A itself is not overruled, just reversed the earlier law.)

Validity status ranking (most severe first, single value per decision):
    1. overruled     — at least one later decision explicitly overrules it
    2. criticized    — criticisms in later decisions
    3. distinguished — later decisions distinguished it (softer)
    4. valid         — default
Confirmations don't change status (a confirmed decision stays valid).

Counts of each treatment are also stored (n_overruled_by, etc.) as
finer-grained signals for the retrieval scorer.
"""

from __future__ import annotations

import sqlite3


# Direction → severity weight. Higher = more damaging to the cited case.
_DIRECTION_WEIGHTS = {
    "overrules":    3,
    "criticizes":   2,
    "distinguishes": 1,
    "confirms":     0,
    "develops":     0,
    "neutral":      0,
}

_STATUS_BY_WEIGHT = {3: "overruled", 2: "criticized", 1: "distinguished", 0: "valid"}


def propagate(conn: sqlite3.Connection) -> dict:
    """Aggregate chunk_case_citations into decision_authority.validity_status
    plus per-treatment counts. Idempotent — re-runs overwrite."""
    # Aggregate per target_decision_id, grouped by direction
    agg = conn.execute("""
        SELECT target_decision_id,
               SUM(CASE WHEN direction = 'overrules'     THEN 1 ELSE 0 END) AS n_overruled,
               SUM(CASE WHEN direction = 'criticizes'    THEN 1 ELSE 0 END) AS n_criticized,
               SUM(CASE WHEN direction = 'distinguishes' THEN 1 ELSE 0 END) AS n_distinguished,
               SUM(CASE WHEN direction = 'confirms'      THEN 1 ELSE 0 END) AS n_confirmed,
               COUNT(*)                                                      AS n_cited
        FROM chunk_case_citations
        WHERE direction IS NOT NULL
        GROUP BY target_decision_id
    """).fetchall()

    cur = conn.cursor()
    n_updated = 0
    for target, n_over, n_crit, n_dist, n_conf, n_cited in agg:
        # Pick the most severe applicable status
        if n_over:   status = "overruled"
        elif n_crit: status = "criticized"
        elif n_dist: status = "distinguished"
        else:        status = "valid"

        # The target may or may not have a decision_authority row yet (if
        # it's in our DB). Insert-or-update; rows for decisions outside
        # our DB won't be referenced by retrieval anyway.
        cur.execute("""
            INSERT INTO decision_authority
                (decision_id, validity_status,
                 n_overruled_by, n_criticized_by, n_confirmed_by, n_cited_by,
                 computed_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(decision_id) DO UPDATE SET
                validity_status  = excluded.validity_status,
                n_overruled_by   = excluded.n_overruled_by,
                n_criticized_by  = excluded.n_criticized_by,
                n_confirmed_by   = excluded.n_confirmed_by,
                n_cited_by       = excluded.n_cited_by,
                computed_at      = datetime('now')
        """, (target, status, n_over, n_crit, n_conf, n_cited))
        n_updated += 1

    conn.commit()
    # Also: set validity_status='valid' for every other decision (no
    # negative treatment observed in our corpus).
    cur.execute("""
        UPDATE decision_authority
        SET validity_status = 'valid'
        WHERE validity_status IS NULL
    """)
    conn.commit()
    return {
        "targets_with_treatment": n_updated,
        "backfilled_valid": cur.rowcount,
    }
