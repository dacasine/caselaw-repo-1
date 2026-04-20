#!/bin/bash
# Weekly entscheidsuche.ch gap-fill — runs Sunday 00:00 UTC.
# Downloads new/updated files, then ingests into JSONL.
# Much heavier than daily scrapers (~247k decisions Tier A).
#
# Cron:
#   0 0 * * 0 /srv/caselaw/weekly_entscheidsuche.sh >> /srv/data/logs/entscheidsuche.log 2>&1
#
set -uo pipefail
cd /srv/caselaw
export PYTHONPATH=/srv/caselaw
export PATH="/srv/caselaw/.venv/bin:$PATH"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
LOCK="/tmp/entscheidsuche.lock"
exec 200>"$LOCK"
flock -n 200 || { log "SKIP: another instance running"; exit 0; }

log "=== ENTSCHEIDSUCHE SYNC START ==="

# Step 1: Download (idempotent — skips existing valid files)
log "▶ Download Tier A+B"
python3 -m scrapers.entscheidsuche_download \
    --dest /srv/data/entscheidsuche \
    --tier-b \
    >> /srv/data/logs/entscheidsuche_download.log 2>&1 || true
log "✓ Download done"

# Step 2: Ingest to JSONL (deduplicates against direct scrapers)
log "▶ Ingest"
python3 -m scrapers.entscheidsuche_ingest \
    --input /srv/data/entscheidsuche \
    --output /srv/caselaw/output/decisions \
    --existing /srv/caselaw/output/decisions \
    >> /srv/data/logs/entscheidsuche_ingest.log 2>&1 || true
log "✓ Ingest done"

log "=== ENTSCHEIDSUCHE SYNC DONE ==="
# The daily_pipeline.sh will pick up the new JSONL next morning
# via publish.py step 1.
