#!/bin/bash
# SAC + Phase5 on OVH — with correct --src-db path
set -euo pipefail
cd /srv/caselaw
export PYTHONPATH=/srv/caselaw

PY=".venv/bin/python"
SRC_DB="/srv/data/decisions.db"
PARAG_DB="/srv/data/parag_chunks.db"
LOG="/srv/data/sac_phase5_ovh.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

SCOPES="bge bger bvger bstger ge_gerichte vd_findinfo ti_gerichte vd_gerichte zh_sozialversicherungsgericht vd_omni zh_obergericht fr_gerichte zh_verwaltungsgericht be_verwaltungsgericht bl_gerichte so_gerichte bs_appellationsgericht sg_versicherungsgericht ne_gerichte be_zivilstraf gr_gerichte vs_gerichte zh_handelsgericht ar_gerichte lu_gerichte sg_gerichte sz_gerichte ag_gerichte sg_verwaltungsgericht finma_versicherungsrecht tg_obergericht ow_gerichte sz_verwaltungsgericht bs_sozialversicherungsgericht be_bvd ag_strafgericht ag_versicherungsgericht edoeb ag_verwaltungsgericht ag_zivilgericht zh_kassationsgericht zh_gerichte gl_gerichte zg_verwaltungsgericht ur_gerichte zg_obergericht zh_baurekursgericht tg_gerichte ag_spezialverwaltungsgericht ju_gerichte nw_gerichte zh_steuerrekursgericht hudoc_ch ubi sg_publikationen be_weitere"

log "=== SAC+PHASE5 START ==="
for scope in $SCOPES; do
    log "▶ SAC $scope"
    $PY scripts/parag/run_sac.py \
        --court "$scope" --workers 32 --rate 800 \
        --provider openrouter --model google/gemini-2.0-flash-001 \
        --src-db "$SRC_DB" --parag-db "$PARAG_DB" || true
    log "✓ SAC $scope"

    log "▶ Phase5 $scope"
    $PY scripts/parag/run_phase5.py \
        --court "$scope" --workers 48 --rate 1000 \
        --model google/gemini-2.0-flash-001 \
        --src-db "$SRC_DB" --parag-db "$PARAG_DB" || true
    log "✓ Phase5 $scope"
done
log "=== ALL DONE ==="
