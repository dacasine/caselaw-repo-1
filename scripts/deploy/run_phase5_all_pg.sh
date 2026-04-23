#!/usr/bin/env bash
# Wait for SAC to finish, then run Phase 5 on all courts with pending enrichment.
# Usage: nohup /srv/caselaw/scripts/deploy/run_phase5_all_pg.sh > /srv/data/logs/phase5_all.log 2>&1 &

set -euo pipefail
cd /srv/caselaw
export PYTHONPATH=/srv/caselaw

PG="docker exec caselaw_pg psql -U caselaw -d caselaw -t -A"
RATE=200
WORKERS=10
MAX_PARALLEL=8

echo "$(date) Waiting for SAC processes to finish..."
while true; do
    n=$(pgrep -cf 'run_sac\.py' 2>/dev/null || true)
    if [ "$n" -eq 0 ]; then
        break
    fi
    echo "  $(date +%H:%M) SAC still running: $n processes"
    sleep 60
done

SAC_DONE=$($PG -c "SELECT count(*) FROM enrichment_state WHERE status='ok';")
echo "$(date) SAC finished. $SAC_DONE decisions enriched."
echo ""

# Get courts with Phase 5 pending (have SAC chunks but no decision_enrichment)
COURTS=$($PG -c "
    SELECT e.court, count(*) AS pending
    FROM enrichment_state e
    LEFT JOIN decision_enrichment de ON de.decision_id = e.decision_id AND de.status = 'ok'
    WHERE e.status = 'ok' AND de.decision_id IS NULL
    GROUP BY e.court
    ORDER BY pending DESC;
")

echo "$(date) Phase 5 pending courts:"
echo "$COURTS"
echo ""

# Launch MAX_PARALLEL at a time
run_court() {
    local court=$1
    echo "  $(date +%H:%M:%S) START phase5 $court"
    .venv/bin/python scripts/parag/run_phase5.py \
        --court "$court" \
        --workers "$WORKERS" \
        --rate "$RATE" \
        --model google/gemini-2.0-flash-001 \
        --fallback google/gemini-2.0-flash-001 \
        > "/srv/data/logs/phase5_pg_${court}.log" 2>&1
    echo "  $(date +%H:%M:%S) DONE  phase5 $court"
}

COURT_LIST=$(echo "$COURTS" | awk -F'|' '{print $1}' | grep -v '^$')
running=0
for court in $COURT_LIST; do
    run_court "$court" &
    running=$((running + 1))
    if [ "$running" -ge "$MAX_PARALLEL" ]; then
        wait -n 2>/dev/null || true
        running=$((running - 1))
    fi
done
wait

echo "$(date) All Phase 5 processes finished."

# Recompute authority scores
echo "$(date) Recomputing authority scores..."
.venv/bin/python scripts/parag/compute_authority.py --skip-pagerank

echo "$(date) DONE."
