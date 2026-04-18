#!/usr/bin/env bash
# Overnight v2 — strict serialisation to avoid SQLite "database is locked".
# NEVER runs embed + Phase5/SAC simultaneously.
#
# Usage:
#   nohup bash scripts/parag/overnight_v2.sh > logs/overnight_v2.log 2>&1 &

set -euo pipefail
cd "$(dirname "$0")/../.."
PY=".venv/bin/python"
export PYTHONPATH="$(pwd)"
export OMP_NUM_THREADS=10
export MKL_NUM_THREADS=10
export TORCH_NUM_THREADS=10

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a logs/overnight_v2_status.log; }

embed_all() {
    log "EMBED: starting (batch=16, threads=10, exclusive writer)"
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
    log "EMBED: done"
}

sac_phase5_scope() {
    local scope="$1"
    log "SAC+P5: starting scope=$scope"
    $PY scripts/parag/run_sac.py \
        --court "$scope" --workers 16 --rate 300 \
        --provider openrouter --model google/gemini-2.0-flash-001
    log "SAC+P5: SAC done for $scope"

    $PY scripts/parag/run_phase5.py \
        --court "$scope" --workers 24 --rate 420 \
        --model google/gemini-2.0-flash-001
    log "SAC+P5: Phase5 done for $scope"
}

# ── 0. Wait for any orphaned Phase 5 to finish ──────────────────────
ORPHAN=$(ps aux | grep "run_phase5" | grep -v grep | awk '{print $2}' | head -1)
if [ -n "$ORPHAN" ]; then
    log "waiting for orphaned Phase 5 (PID $ORPHAN)..."
    while kill -0 "$ORPHAN" 2>/dev/null; do sleep 30; done
    log "orphan done."
fi

# ── 1. Embed ALL pending (exclusive writer, no contention) ──────────
embed_all

# ── 2. SAC + Phase 5 for each remaining scope (NO embed) ────────────
# Then embed after each batch of scopes to catch up.
SCOPES_FEDERAL="bstger"
SCOPES_CANTONAL_BIG="ge_gerichte vd_findinfo ti_gerichte vd_gerichte zh_sozialversicherungsgericht vd_omni zh_obergericht"
SCOPES_CANTONAL_MED="fr_gerichte zh_verwaltungsgericht be_verwaltungsgericht bl_gerichte so_gerichte bs_appellationsgericht sg_versicherungsgericht ne_gerichte"
SCOPES_CANTONAL_SMALL="be_zivilstraf gr_gerichte vs_gerichte zh_handelsgericht ar_gerichte lu_gerichte sg_gerichte sz_gerichte ag_gerichte sg_verwaltungsgericht finma_versicherungsrecht tg_obergericht ow_gerichte sz_verwaltungsgericht bs_sozialversicherungsgericht be_bvd ag_strafgericht ag_versicherungsgericht edoeb ag_verwaltungsgericht ag_zivilgericht zh_kassationsgericht zh_gerichte gl_gerichte zg_verwaltungsgericht ur_gerichte zg_obergericht zh_baurekursgericht tg_gerichte ag_spezialverwaltungsgericht ju_gerichte nw_gerichte zh_steuerrekursgericht hudoc_ch ubi sg_publikationen be_weitere"

# Federal
for scope in $SCOPES_FEDERAL; do
    sac_phase5_scope "$scope"
done
log "federal scopes complete."

# Cantonal big — then embed catch-up
for scope in $SCOPES_CANTONAL_BIG; do
    sac_phase5_scope "$scope"
done
log "cantonal big scopes complete. embedding catch-up..."
embed_all

# Cantonal medium — then embed catch-up
for scope in $SCOPES_CANTONAL_MED; do
    sac_phase5_scope "$scope"
done
log "cantonal medium scopes complete. embedding catch-up..."
embed_all

# Cantonal small — then final embed
for scope in $SCOPES_CANTONAL_SMALL; do
    sac_phase5_scope "$scope"
done
log "cantonal small scopes complete. final embedding..."
embed_all

log "=== ALL DONE ==="
