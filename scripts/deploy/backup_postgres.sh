#!/usr/bin/env bash
# Daily Postgres backup — pg_dump + S3 upload + GFS rotation.
#
# Retention (Grandfather-Father-Son):
#   - Daily:   keep 7 days
#   - Weekly:  keep 30 days  (Sunday backup promoted)
#   - Monthly: keep 365 days (1st-of-month backup promoted)
#
# Requires S3 credentials in /srv/caselaw/.env:
#   S3_ENDPOINT, S3_BUCKET, S3_ACCESS_KEY, S3_SECRET_KEY
#
# Cron:
#   30 3 * * * /srv/caselaw/scripts/deploy/backup_postgres.sh >> /srv/data/logs/backup_pg.log 2>&1

set -euo pipefail

# ── Load .env ────────────────────────────────────────────────
ENV_FILE="/srv/caselaw/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

# ── Config ───────────────────────────────────────────────────
BACKUP_DIR="/srv/data/backups/postgres"
CONTAINER="caselaw_pg"
PG_USER="caselaw"
PG_DB="caselaw"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DAY_OF_WEEK=$(date +%u)   # 1=Mon … 7=Sun
DAY_OF_MONTH=$(date +%d)
AWS="/srv/caselaw/.venv/bin/aws"

mkdir -p "$BACKUP_DIR"

# Determine tier: monthly > weekly > daily
if [ "$DAY_OF_MONTH" = "01" ]; then
    TIER="monthly"
elif [ "$DAY_OF_WEEK" = "7" ]; then
    TIER="weekly"
else
    TIER="daily"
fi

BACKUP_FILE="${BACKUP_DIR}/caselaw_${TIER}_${TIMESTAMP}.sql.gz"

# ── 1. pg_dump ───────────────────────────────────────────────
echo "$(date '+%Y-%m-%d %H:%M:%S') [backup] tier=${TIER} → ${BACKUP_FILE}"

docker exec "$CONTAINER" pg_dump \
    -U "$PG_USER" \
    -d "$PG_DB" \
    --no-owner \
    --no-privileges \
    2>/dev/null \
  | gzip -6 > "$BACKUP_FILE"

SIZE=$(du -sh "$BACKUP_FILE" | cut -f1)
echo "$(date '+%Y-%m-%d %H:%M:%S') [backup] pg_dump complete: ${SIZE}"

# Row-count snapshot
docker exec "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -c \
    "SELECT relname, n_live_tup FROM pg_stat_user_tables WHERE schemaname='public' ORDER BY n_live_tup DESC;" \
    > "${BACKUP_DIR}/caselaw_${TIER}_${TIMESTAMP}_counts.txt" 2>/dev/null

# ── 2. Upload to S3 ─────────────────────────────────────────
if [ -n "${S3_ENDPOINT:-}" ] && [ -n "${S3_ACCESS_KEY:-}" ] && [ "${S3_ACCESS_KEY}" != "CHANGE_ME" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [backup] Uploading to S3: ${S3_BUCKET}/${TIER}/"

    export AWS_ACCESS_KEY_ID="$S3_ACCESS_KEY"
    export AWS_SECRET_ACCESS_KEY="$S3_SECRET_KEY"

    S3_PREFIX="s3://${S3_BUCKET}/postgres/${TIER}"

    "$AWS" s3 cp "$BACKUP_FILE" \
        "${S3_PREFIX}/caselaw_${TIER}_${TIMESTAMP}.sql.gz" \
        --endpoint-url "$S3_ENDPOINT" --no-progress 2>&1 && S3_OK=1 || S3_OK=0

    if [ "$S3_OK" = "1" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') [backup] S3 upload OK"

        "$AWS" s3 cp "${BACKUP_DIR}/caselaw_${TIER}_${TIMESTAMP}_counts.txt" \
            "${S3_PREFIX}/caselaw_${TIER}_${TIMESTAMP}_counts.txt" \
            --endpoint-url "$S3_ENDPOINT" --no-progress 2>/dev/null || true

        # S3 GFS rotation per tier
        _s3_rotate() {
            local tier=$1 keep_days=$2
            local cutoff
            cutoff=$(date -d "-${keep_days} days" +%Y%m%d 2>/dev/null || date -v-${keep_days}d +%Y%m%d 2>/dev/null || echo "")
            [ -z "$cutoff" ] && return
            "$AWS" s3 ls "s3://${S3_BUCKET}/postgres/${tier}/" \
                --endpoint-url "$S3_ENDPOINT" 2>/dev/null \
              | while read -r line; do
                    fname=$(echo "$line" | awk '{print $NF}')
                    fdate=$(echo "$fname" | grep -oP "${tier}_\K\d{8}" 2>/dev/null || echo "")
                    if [ -n "$fdate" ] && [ "$fdate" -lt "$cutoff" ] 2>/dev/null; then
                        "$AWS" s3 rm "s3://${S3_BUCKET}/postgres/${tier}/${fname}" \
                            --endpoint-url "$S3_ENDPOINT" 2>/dev/null && \
                            echo "  S3 deleted: ${tier}/${fname}"
                    fi
                done
        }
        _s3_rotate daily   7
        _s3_rotate weekly  30
        _s3_rotate monthly 365
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') [backup] S3 upload FAILED — local only"
    fi
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') [backup] S3 not configured (S3_ACCESS_KEY=CHANGE_ME)"
fi

# ── 3. Local GFS rotation ───────────────────────────────────
find "$BACKUP_DIR" -name "caselaw_daily_*"   -mtime +7   -delete 2>/dev/null
find "$BACKUP_DIR" -name "caselaw_weekly_*"  -mtime +30  -delete 2>/dev/null
find "$BACKUP_DIR" -name "caselaw_monthly_*" -mtime +365 -delete 2>/dev/null

echo "$(date '+%Y-%m-%d %H:%M:%S') [backup] Local inventory:"
for t in daily weekly monthly; do
    n=$(ls -1 "$BACKUP_DIR"/caselaw_${t}_*.sql.gz 2>/dev/null | wc -l)
    echo "  ${t}: ${n} backups"
done
echo "---"
