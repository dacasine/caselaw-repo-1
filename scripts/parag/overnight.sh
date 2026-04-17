#!/usr/bin/env bash
# Unattended overnight pipeline. Sequenced to avoid SQLite write contention.
#
# Phase 5 (OpenRouter LLM, network-bound) and Embed (CPU-bound) compete
# for the single WAL writer lock on parag_chunks.db. Running them
# together cuts embed speed by ~40 %. This script serialises them:
#
#   1. Wait for current Phase 5 BGer (orphan process) to finish
#   2. Embed ALL pending chunks (batch=16, threads=10, exclusive writer)
#   3. Pipeline SAC+Phase5 (skip-embed) for remaining scopes
#   4. Final embed pass for new chunks created in step 3
#
# Usage:
#   nohup bash scripts/parag/overnight.sh > logs/overnight.log 2>&1 &
#   echo $! > logs/overnight.pid

set -euo pipefail

cd "$(dirname "$0")/../.."
PY=".venv/bin/python"
export PYTHONPATH="$(pwd)"
export OMP_NUM_THREADS=10
export MKL_NUM_THREADS=10
export TORCH_NUM_THREADS=10

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# ── 0. Kill the old pipeline (we replace it) ────────────────────────
OLD_PID=$(cat logs/pipeline.pid 2>/dev/null || echo "")
if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    log "killing old pipeline PID $OLD_PID"
    kill "$OLD_PID" 2>/dev/null || true
    sleep 2
fi

# ── 1. Wait for orphaned Phase 5 BGer to finish ─────────────────────
PHASE5_PID=$(ps aux | grep "run_phase5.*bger" | grep -v grep | awk '{print $2}' | head -1)
if [ -n "$PHASE5_PID" ]; then
    log "waiting for Phase 5 BGer (PID $PHASE5_PID) to finish..."
    while kill -0 "$PHASE5_PID" 2>/dev/null; do sleep 30; done
    log "Phase 5 BGer done."
else
    log "no Phase 5 BGer process found — already finished."
fi

# ── 2. Embed ALL pending chunks (exclusive writer = full speed) ──────
log "starting embed pass (batch=16, threads=10, all scopes)"
$PY -c "
import torch, sys, os, json
torch.set_num_threads(10)
torch.set_num_interop_threads(10)

sys.path.insert(0, os.getcwd())
from db_schema_parag import DEFAULT_PARAG_DB, init_parag_schema
from search_stack.parag.embedder import encode_and_store, load_embedder

conn = init_parag_schema(DEFAULT_PARAG_DB)
model = load_embedder(device='cpu')
stats = encode_and_store(conn, model, batch_size=16, progress_every=5000)
print(json.dumps(stats, indent=2, default=str), flush=True)
conn.close()
"
log "embed pass 1 done."

# ── 3. Pipeline SAC + Phase 5 for remaining scopes (skip embed) ──────
log "starting SAC + Phase 5 for remaining scopes (skip embed)"
$PY scripts/parag/run_pipeline.py \
    --skip-embed \
    --scopes \
        bvger bstger \
        ge_gerichte vd_findinfo ti_gerichte vd_gerichte \
        zh_sozialversicherungsgericht vd_omni zh_obergericht \
        fr_gerichte zh_verwaltungsgericht be_verwaltungsgericht bl_gerichte \
        so_gerichte bs_appellationsgericht sg_versicherungsgericht ne_gerichte \
        be_zivilstraf gr_gerichte vs_gerichte zh_handelsgericht ar_gerichte \
        lu_gerichte sg_gerichte sz_gerichte ag_gerichte sg_verwaltungsgericht \
        finma_versicherungsrecht tg_obergericht ow_gerichte sz_verwaltungsgericht \
        bs_sozialversicherungsgericht be_bvd ag_strafgericht ag_versicherungsgericht \
        edoeb ag_verwaltungsgericht ag_zivilgericht zh_kassationsgericht \
        zh_gerichte gl_gerichte zg_verwaltungsgericht ur_gerichte zg_obergericht \
        zh_baurekursgericht tg_gerichte ag_spezialverwaltungsgericht ju_gerichte \
        nw_gerichte zh_steuerrekursgericht hudoc_ch ubi sg_publikationen be_weitere \
    --sac-workers 16 --sac-rate 300 \
    --phase5-workers 24 --phase5-rate 420
log "SAC + Phase 5 pipeline done."

# ── 4. Final embed pass (catches chunks created in step 3) ───────────
log "starting final embed pass"
$PY -c "
import torch, sys, os, json
torch.set_num_threads(10); torch.set_num_interop_threads(10)
sys.path.insert(0, os.getcwd())
from db_schema_parag import DEFAULT_PARAG_DB, init_parag_schema
from search_stack.parag.embedder import encode_and_store, load_embedder
conn = init_parag_schema(DEFAULT_PARAG_DB)
model = load_embedder(device='cpu')
stats = encode_and_store(conn, model, batch_size=16, progress_every=5000)
print(json.dumps(stats, indent=2, default=str), flush=True)
conn.close()
"
log "final embed pass done."

log "=== ALL DONE ==="
