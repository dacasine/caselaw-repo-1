#!/bin/bash
# Daily ingestion + PA-RAG enrichment pipeline (Postgres).
# Designed for cron: runs unattended, logs everything, idempotent.
#
# Flow:
#   1. Scrape all courts (→ JSONL files)
#   2. Publish (ingest JSONL → decisions table in Postgres)
#   3. SAC on new decisions (parse + chunk + summarize)
#   4. Phase 5 on new decisions (LLM enrichment)
#   5. Embed new chunks (BGE-M3 → pgvector)
#   6. Update authority scores
#
# Cron:
#   0 2 * * * /srv/caselaw/scripts/deploy/daily_pipeline.sh >> /srv/data/logs/daily_pipeline.log 2>&1

set -uo pipefail
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

# ── Step 1: Scrape all courts ────────────────────────────────
log "▶ Step 1: Scraping"
python3 run_all_scrapers.py \
    --parallel 4 --timeout 7200 \
    --exclude ow_gerichte,ju_gerichte \
    >> "$LOG_DIR/scrape_$(date +%Y%m%d).log" 2>&1 || true
log "✓ Step 1 done"

# ── Step 2: Publish (ingest JSONL → decisions.db) ────────────
# publish.py still writes to SQLite decisions.db — that's fine,
# we sync new decisions to Postgres in step 2b.
log "▶ Step 2a: Publish to SQLite"
python3 publish.py \
    --fast-only \
    >> "$LOG_DIR/publish_$(date +%Y%m%d).log" 2>&1 || true

log "▶ Step 2b: Sync new decisions SQLite → Postgres"
python3 -c "
import sqlite3, psycopg, os, sys
sys.path.insert(0, '/srv/caselaw')
from search_stack.parag.pg_conn import get_pg_url, _load_env
_load_env()

src = sqlite3.connect('file:///srv/data/decisions.db?mode=ro', uri=True)
pg = psycopg.connect(get_pg_url(), autocommit=False)

# Find decisions in SQLite not yet in Postgres
existing = set(r[0] for r in pg.execute('SELECT decision_id FROM decisions').fetchall())
rows = src.execute(
    'SELECT decision_id, court, canton, chamber, docket_number, docket_number_2, '
    'decision_date, publication_date, language, title, legal_area, regeste, '
    'abstract_de, abstract_fr, abstract_it, full_text, decision_type, outcome, '
    'source_url, pdf_url, cited_decisions, scraped_at, source, source_id, '
    'source_spider, content_hash, json_data, canonical_key FROM decisions'
).fetchall()

new = [r for r in rows if r[0] not in existing]
if new:
    with pg.cursor() as cur:
        for r in new:
            cleaned = list(r)
            # Fix dates
            for i in [6, 7, 21]:
                if isinstance(cleaned[i], str) and (cleaned[i].strip() == '' or cleaned[i].startswith('0000')):
                    cleaned[i] = None
            # Fix NUL bytes
            for i, v in enumerate(cleaned):
                if isinstance(v, str) and '\x00' in v:
                    cleaned[i] = v.replace('\x00', '')
            cur.execute(
                'INSERT INTO decisions (decision_id, court, canton, chamber, docket_number, '
                'docket_number_2, decision_date, publication_date, language, title, legal_area, '
                'regeste, abstract_de, abstract_fr, abstract_it, full_text, decision_type, '
                'outcome, source_url, pdf_url, cited_decisions, scraped_at, source, source_id, '
                'source_spider, content_hash, json_data, canonical_key) '
                'VALUES (' + ','.join(['%s']*28) + ') ON CONFLICT (decision_id, decision_year) DO NOTHING',
                cleaned,
            )
    pg.commit()
    print(f'Synced {len(new)} new decisions to Postgres', flush=True)
else:
    print('No new decisions to sync', flush=True)
src.close()
pg.close()
" >> "$LOG_DIR/sync_$(date +%Y%m%d).log" 2>&1 || true
log "✓ Step 2 done"

# ── Step 3: SAC on new decisions ─────────────────────────────
log "▶ Step 3: SAC (new decisions)"
# Get courts that have unprocessed decisions
COURTS=$(docker exec caselaw_pg psql -U caselaw -d caselaw -t -A -c "
    SELECT DISTINCT d.court FROM decisions d
    LEFT JOIN enrichment_state e ON e.decision_id = d.decision_id AND e.status = 'ok'
    WHERE d.full_text IS NOT NULL AND length(d.full_text) > 500
      AND e.decision_id IS NULL
    LIMIT 50;
" 2>/dev/null)

if [ -n "$COURTS" ]; then
    for court in $COURTS; do
        python3 scripts/parag/run_sac.py \
            --court "$court" --workers 8 --rate 200 \
            --provider openrouter \
            >> "$LOG_DIR/sac_daily_$(date +%Y%m%d).log" 2>&1 || true
    done
fi
log "✓ Step 3 done"

# ── Step 4: Phase 5 on new decisions ─────────────────────────
log "▶ Step 4: Phase 5 (new decisions)"
COURTS_P5=$(docker exec caselaw_pg psql -U caselaw -d caselaw -t -A -c "
    SELECT DISTINCT e.court FROM enrichment_state e
    LEFT JOIN decision_enrichment de ON de.decision_id = e.decision_id
        AND de.status IN ('ok', 'schema_invalid')
    WHERE e.status = 'ok' AND de.decision_id IS NULL
    LIMIT 50;
" 2>/dev/null)

if [ -n "$COURTS_P5" ]; then
    for court in $COURTS_P5; do
        python3 scripts/parag/run_phase5.py \
            --court "$court" --workers 8 --rate 200 \
            --model google/gemini-2.0-flash-001 \
            --fallback google/gemini-2.0-flash-001 \
            >> "$LOG_DIR/phase5_daily_$(date +%Y%m%d).log" 2>&1 || true
    done
fi
log "✓ Step 4 done"

# ── Step 5: Embed new chunks ────────────────────────────────
log "▶ Step 5: Embed new chunks"
OMP_NUM_THREADS=12 MKL_NUM_THREADS=12 \
python3 scripts/parag/run_embed.py \
    --batch 64 \
    >> "$LOG_DIR/embed_daily_$(date +%Y%m%d).log" 2>&1 || true
log "✓ Step 5 done"

# ── Step 6: Update authority scores ──────────────────────────
log "▶ Step 6: Authority + validity"
python3 scripts/parag/compute_authority.py \
    --skip-pagerank \
    >> "$LOG_DIR/authority_daily_$(date +%Y%m%d).log" 2>&1 || true
log "✓ Step 6 done"

log "=== DAILY PIPELINE DONE ==="
