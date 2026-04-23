#!/usr/bin/env bash
# Run SAC in parallel across multiple courts — Postgres multi-writer.
# Each court gets its own process with dedicated workers.
#
# Usage:
#   /srv/caselaw/scripts/deploy/run_sac_parallel_pg.sh

set -euo pipefail
cd /srv/caselaw
export PYTHONPATH=/srv/caselaw

LOGDIR="/srv/data/logs"
RATE=120         # per-process LLM rate
WORKERS=6        # workers per process
MAX_PARALLEL=4   # max simultaneous court processes

# All courts to process, ordered by pending volume
COURTS=(
    bger ge_gerichte bvger vd_findinfo ti_gerichte vd_gerichte bge
    zh_sozialversicherungsgericht zh_obergericht ch_vb fr_gerichte
    zh_verwaltungsgericht bstger be_verwaltungsgericht bl_gerichte
    so_gerichte bs_appellationsgericht sg_versicherungsgericht ne_gerichte
)

echo "$(date) Starting SAC for ${#COURTS[@]} courts (rate=${RATE}/proc, workers=${WORKERS}/proc, max_parallel=${MAX_PARALLEL})"

run_court() {
    local court=$1
    local LOG="${LOGDIR}/sac_pg_${court}.log"
    echo "  $(date +%H:%M:%S) START $court"
    .venv/bin/python scripts/parag/run_sac.py \
        --court "$court" \
        --workers "$WORKERS" \
        --rate "$RATE" \
        --provider openrouter \
        > "$LOG" 2>&1
    echo "  $(date +%H:%M:%S) DONE  $court"
}

# Run MAX_PARALLEL at a time using a job queue
running=0
for court in "${COURTS[@]}"; do
    run_court "$court" &
    running=$((running + 1))
    if [ "$running" -ge "$MAX_PARALLEL" ]; then
        wait -n 2>/dev/null || true
        running=$((running - 1))
    fi
done
wait

echo "$(date) All SAC processes finished."
