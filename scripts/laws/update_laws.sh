#!/usr/bin/env bash
# Weekly law update — scrape Fedlex + LexFind, diff, upsert versions.
# Cron: 0 1 * * 0 /srv/caselaw/scripts/laws/update_laws.sh >> /srv/data/logs/update_laws.log 2>&1
set -uo pipefail
cd /srv/caselaw
export PYTHONPATH=/srv/caselaw
export PATH="/srv/caselaw/.venv/bin:$PATH"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === LAW UPDATE START ==="
python3 scripts/laws/update_laws.py 2>&1
echo "[$(date '+%Y-%m-%d %H:%M:%S')] === LAW UPDATE DONE ==="
