"""Propagate validity_status from Phase 5 prior_case_treatment.

Every decision's Phase 5 enrichment stores a `prior_case_treatment`
array describing how THIS decision treats earlier arrêts (confirms,
overrules, criticizes, distinguishes, develops, neutral).

This module inverts that graph to derive the target decisions' status.
Now reads/writes Postgres via psycopg.
"""

from __future__ import annotations

import psycopg


_DIRECTION_WEIGHTS = {
    "overrules":    3,
    "criticizes":   2,
    "distinguishes": 1,
    "confirms":     0,
    "develops":     0,
    "neutral":      0,
}

_STATUS_BY_WEIGHT = {3: "overruled", 2: "criticized", 1: "distinguished", 0: "valid"}


def propagate(conn: psycopg.Connection) -> dict:
    """Aggregate chunk_case_citations into decision_authority.validity_status
    plus per-treatment counts. Idempotent."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT target_decision_id,
                   SUM(CASE WHEN direction = 'overrules'     THEN 1 ELSE 0 END) AS n_overruled,
                   SUM(CASE WHEN direction = 'criticizes'    THEN 1 ELSE 0 END) AS n_criticized,
                   SUM(CASE WHEN direction = 'distinguishes' THEN 1 ELSE 0 END) AS n_distinguished,
                   SUM(CASE WHEN direction = 'confirms'      THEN 1 ELSE 0 END) AS n_confirmed,
                   COUNT(*)                                                      AS n_cited
            FROM chunk_case_citations
            WHERE direction IS NOT NULL
            GROUP BY target_decision_id
        """)
        agg = cur.fetchall()

    n_updated = 0
    with conn.cursor() as cur:
        for target, n_over, n_crit, n_dist, n_conf, n_cited in agg:
            if n_over:   status = "overruled"
            elif n_crit: status = "criticized"
            elif n_dist: status = "distinguished"
            else:        status = "valid"

            cur.execute("""
                INSERT INTO decision_authority
                    (decision_id, validity_status,
                     n_overruled_by, n_criticized_by, n_confirmed_by, n_cited_by,
                     computed_at)
                VALUES (%s, %s, %s, %s, %s, %s, now())
                ON CONFLICT(decision_id) DO UPDATE SET
                    validity_status  = EXCLUDED.validity_status,
                    n_overruled_by   = EXCLUDED.n_overruled_by,
                    n_criticized_by  = EXCLUDED.n_criticized_by,
                    n_confirmed_by   = EXCLUDED.n_confirmed_by,
                    n_cited_by       = EXCLUDED.n_cited_by,
                    computed_at      = now()
            """, (target, status, n_over, n_crit, n_conf, n_cited))
            n_updated += 1

    conn.commit()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE decision_authority
            SET validity_status = 'valid'
            WHERE validity_status IS NULL
        """)
        backfilled = cur.rowcount
    conn.commit()
    return {
        "targets_with_treatment": n_updated,
        "backfilled_valid": backfilled,
    }
