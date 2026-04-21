#!/bin/bash
# Daily ingestion + PA-RAG enrichment pipeline for OVH.
# Designed for cron: runs unattended, logs everything, idempotent.
#
# Cron entry:
#   0 2 * * * /srv/caselaw/daily_pipeline.sh >> /srv/data/logs/daily_pipeline.log 2>&1
#
set -uo pipefail  # no -e: individual failures shouldn't stop the chain
cd /srv/caselaw
export PYTHONPATH=/srv/caselaw
export PATH="/srv/caselaw/.venv/bin:$PATH"

LOG_DIR="/srv/data/logs"
mkdir -p "$LOG_DIR" output/decisions state

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# Lock: only one instance at a time
LOCK="/tmp/daily_pipeline.lock"
exec 200>"$LOCK"
flock -n 200 || { log "SKIP: another instance running"; exit 0; }

log "=== DAILY PIPELINE START ==="

# ── Step 1: Scrape all courts ────────────────────────────────────────
log "▶ Step 1: Scraping"
python3 run_all_scrapers.py \
    --parallel 4 --timeout 7200 \
    --exclude ow_gerichte,ju_gerichte \
    >> "$LOG_DIR/scrape_$(date +%Y%m%d).log" 2>&1 || true
log "✓ Step 1 done"

# ── Step 2: Publish (ingest JSONL → decisions.db + reference graph) ──
log "▶ Step 2: Publish"
python3 publish.py \
    --skip-hf-upload \
    >> "$LOG_DIR/publish_$(date +%Y%m%d).log" 2>&1 || true
log "✓ Step 2 done"

# ── Step 3: SAC on new decisions ─────────────────────────────────────
# run_sac.py is idempotent — skips decisions already chunked.
# We run it on ALL courts; it'll only process newly scraped decisions.
log "▶ Step 3: SAC (new decisions)"
COURTS=$(sqlite3 /srv/data/decisions.db "SELECT DISTINCT court FROM decisions" 2>/dev/null | tr '\n' ' ')
for court in $COURTS; do
    python3 scripts/parag/run_sac.py \
        --court "$court" --workers 8 --rate 200 \
        --provider openrouter --model google/gemini-2.0-flash-001 \
        --src-db /srv/data/decisions.db \
        --parag-db /srv/data/parag_chunks.db || true
done
log "✓ Step 3 done"

# ── Step 4: Phase 5 on new decisions ─────────────────────────────────
log "▶ Step 4: Phase 5 (new decisions)"
for court in $COURTS; do
    python3 scripts/parag/run_phase5.py \
        --court "$court" --workers 8 --rate 200 \
        --model google/gemini-2.0-flash-001 \
        --src-db /srv/data/decisions.db \
        --parag-db /srv/data/parag_chunks.db || true
done
log "✓ Step 4 done"

# ── Step 5: Embed new chunks ────────────────────────────────────────
log "▶ Step 5: Embed new chunks"
OMP_NUM_THREADS=12 MKL_NUM_THREADS=12 \
python3 -c "
import torch, sys, json
torch.set_num_threads(12)
sys.path.insert(0, '/srv/caselaw')
from db_schema_parag import init_parag_schema
from search_stack.parag.embedder import encode_and_store, load_embedder
conn = init_parag_schema('/srv/data/parag_chunks.db')
model = load_embedder(device='cpu')
stats = encode_and_store(conn, model, batch_size=64, progress_every=1000)
print(json.dumps(stats, default=str), flush=True)
conn.close()
" || true
log "✓ Step 5 done"

# ── Step 6: Update authority scores ──────────────────────────────────
log "▶ Step 6: Authority + PageRank"
python3 scripts/parag/compute_authority.py \
    --parag-db /srv/data/parag_chunks.db || true
log "✓ Step 6 done"

# ── Step 7: Freshness check ─────────────────────────────────────────
log "▶ Step 7: Freshness check"
python3 scripts/check_scraper_freshness.py 2>&1 | tail -20 || true
log "✓ Step 7 done"

log "=== DAILY PIPELINE DONE ==="
