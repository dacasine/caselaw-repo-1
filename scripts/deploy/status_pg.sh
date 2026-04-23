#!/usr/bin/env bash
# Postgres PA-RAG status dashboard.
# Usage: bash /srv/caselaw/scripts/deploy/status_pg.sh

set -euo pipefail

PG="docker exec caselaw_pg psql -U caselaw -d caselaw -t -A"

echo "═══════════════════════════════════════════════════════════════"
echo "  PA-RAG Postgres Dashboard  —  $(date '+%Y-%m-%d %H:%M:%S')"
echo "═══════════════════════════════════════════════════════════════"

echo ""
echo "▸ DATABASE SIZE"
$PG -c "SELECT pg_size_pretty(pg_database_size('caselaw'));"

echo ""
echo "▸ TABLE ROW COUNTS"
$PG -c "
SELECT relname AS table, to_char(n_live_tup, 'FM999,999,999') AS rows
FROM pg_stat_user_tables
WHERE schemaname='public' AND n_live_tup > 0
ORDER BY n_live_tup DESC;
" | column -t -s'|'

echo ""
echo "▸ SAC COVERAGE (Phase 3)"
$PG -c "
WITH eligible AS (
    SELECT court, count(*) AS total
    FROM decisions
    WHERE full_text IS NOT NULL AND length(full_text) > 500
    GROUP BY court
),
done AS (
    SELECT court, count(*) AS done
    FROM enrichment_state
    WHERE status = 'ok'
    GROUP BY court
)
SELECT e.court,
       to_char(e.total, 'FM999,999') AS eligible,
       to_char(COALESCE(d.done, 0), 'FM999,999') AS sac_done,
       to_char(e.total - COALESCE(d.done, 0), 'FM999,999') AS pending,
       CASE WHEN e.total > 0
            THEN round(100.0 * COALESCE(d.done, 0) / e.total, 1)::text || '%'
            ELSE '0%' END AS pct
FROM eligible e
LEFT JOIN done d ON d.court = e.court
WHERE e.total - COALESCE(d.done, 0) > 0
ORDER BY e.total - COALESCE(d.done, 0) DESC
LIMIT 25;
" | column -t -s'|'

echo ""
$PG -c "
SELECT
  to_char(count(*), 'FM999,999') AS total_eligible,
  to_char(sum(CASE WHEN e.status = 'ok' THEN 1 ELSE 0 END), 'FM999,999') AS sac_done,
  to_char(count(*) - sum(CASE WHEN e.status = 'ok' THEN 1 ELSE 0 END), 'FM999,999') AS pending,
  round(100.0 * sum(CASE WHEN e.status = 'ok' THEN 1 ELSE 0 END) / count(*), 1)::text || '%' AS pct
FROM decisions d
LEFT JOIN enrichment_state e ON e.decision_id = d.decision_id
WHERE d.full_text IS NOT NULL AND length(d.full_text) > 500;
" | while IFS='|' read -r total done pending pct; do
    echo "  TOTAL: $total eligible | $done SAC done | $pending pending | $pct"
done

echo ""
echo "▸ PHASE 5 ENRICHMENT"
$PG -c "
SELECT status, to_char(count(*), 'FM999,999') AS n
FROM decision_enrichment
GROUP BY status ORDER BY count(*) DESC;
" | column -t -s'|'
$PG -c "
WITH sac AS (
    SELECT count(DISTINCT decision_id) AS n FROM enrichment_state WHERE status='ok'
),
p5 AS (
    SELECT count(*) AS n FROM decision_enrichment WHERE status='ok'
)
SELECT to_char(s.n, 'FM999,999') AS sac_done,
       to_char(p.n, 'FM999,999') AS phase5_done,
       to_char(s.n - p.n, 'FM999,999') AS phase5_pending
FROM sac s, p5 p;
" | while IFS='|' read -r sac p5 pending; do
    echo "  SAC done: $sac | Phase 5 done: $p5 | Phase 5 pending: $pending"
done

echo ""
echo "▸ EMBEDDINGS"
$PG -c "
WITH pending AS (
    SELECT count(*) AS n FROM chunks c
    LEFT JOIN chunk_embeddings ce ON ce.chunk_id = c.id
    WHERE ce.chunk_id IS NULL AND c.summary_source != 'stub' AND length(c.cleaned) > 50
),
done AS (
    SELECT count(*) AS n FROM chunk_embeddings
)
SELECT to_char(d.n, 'FM999,999') AS embedded,
       to_char(p.n, 'FM999,999') AS pending,
       CASE WHEN d.n + p.n > 0
            THEN round(100.0 * d.n / (d.n + p.n), 1)::text || '%'
            ELSE '0%' END AS pct
FROM done d, pending p;
" | while IFS='|' read -r done pending pct; do
    echo "  Embedded: $done | Pending: $pending | Coverage: $pct"
done

echo ""
echo "▸ CITATION GRAPH"
$PG -c "
SELECT 'decision_citations' AS tbl, to_char(count(*), 'FM999,999,999') AS n FROM decision_citations
UNION ALL
SELECT 'decision_statutes', to_char(count(*), 'FM999,999,999') FROM decision_statutes
UNION ALL
SELECT 'chunk_law_citations', to_char(count(*), 'FM999,999,999') FROM chunk_law_citations
UNION ALL
SELECT 'chunk_case_citations', to_char(count(*), 'FM999,999,999') FROM chunk_case_citations;
" | column -t -s'|'

echo ""
echo "▸ LAWS"
$PG -c "
SELECT 'federal_laws' AS tbl, to_char(count(*), 'FM999,999') AS n FROM laws_federal
UNION ALL SELECT 'federal_articles', to_char(count(*), 'FM999,999') FROM articles_federal
UNION ALL SELECT 'cantonal_laws', to_char(count(*), 'FM999,999') FROM laws_cantonal
UNION ALL SELECT 'cantonal_articles', to_char(count(*), 'FM999,999') FROM articles_cantonal;
" | column -t -s'|'

echo ""
echo "▸ ACTIVE PROCESSES"
ps aux | grep -E 'run_sac|run_phase5|run_embed|sqlite_to_pg|backup_postgres' | grep python | grep -v grep \
    | awk '{printf "  PID %-8s RSS %-8s %s\n", $2, int($6/1024)"MB", $11" "$12" "$13}' || echo "  (none)"

echo ""
echo "▸ DISK"
df -h / | tail -1 | awk '{printf "  Total: %s  Used: %s  Free: %s  (%s)\n", $2, $3, $4, $5}'
$PG -c "SELECT pg_size_pretty(pg_database_size('caselaw'));" | while read -r s; do
    echo "  Postgres DB: $s"
done

echo ""
echo "═══════════════════════════════════════════════════════════════"
