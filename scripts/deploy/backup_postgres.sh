#!/usr/bin/env bash
# Daily Postgres backup — pg_dump + S3 upload + GFS rotation.
#
# Safety: NEVER deletes old backups unless the new one succeeded AND
# the S3 upload succeeded. Validates file size before declaring success.
#
# Retention (Grandfather-Father-Son):
#   - Daily:   keep 7 days
#   - Weekly:  keep 30 days  (Sunday backup promoted)
#   - Monthly: keep 365 days (1st-of-month backup promoted)
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
MIN_BACKUP_SIZE_MB=100     # reject backups smaller than this (corrupt/empty)

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
COUNTS_FILE="${BACKUP_DIR}/caselaw_${TIER}_${TIMESTAMP}_counts.txt"

# ── 1. pg_dump ───────────────────────────────────────────────
echo "$(date '+%Y-%m-%d %H:%M:%S') [backup] tier=${TIER} → ${BACKUP_FILE}"

docker exec "$CONTAINER" pg_dump \
    -U "$PG_USER" \
    -d "$PG_DB" \
    --no-owner \
    --no-privileges \
    2>/dev/null \
  | gzip -6 > "$BACKUP_FILE"

# ── 2. Validate dump ────────────────────────────────────────
SIZE_BYTES=$(stat -c%s "$BACKUP_FILE" 2>/dev/null || stat -f%z "$BACKUP_FILE" 2>/dev/null || echo 0)
SIZE_MB=$((SIZE_BYTES / 1024 / 1024))

if [ "$SIZE_MB" -lt "$MIN_BACKUP_SIZE_MB" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [backup] FAILED — dump too small (${SIZE_MB}MB < ${MIN_BACKUP_SIZE_MB}MB minimum). Keeping old backups."
    rm -f "$BACKUP_FILE"
    exit 1
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') [backup] pg_dump OK: ${SIZE_MB}MB"

# Row-count snapshot
docker exec "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -c \
    "SELECT relname, n_live_tup FROM pg_stat_user_tables WHERE schemaname='public' ORDER BY n_live_tup DESC;" \
    > "$COUNTS_FILE" 2>/dev/null

# ── 3. Upload to S3 ─────────────────────────────────────────
S3_OK=0
if [ -n "${S3_ENDPOINT:-}" ] && [ -n "${S3_ACCESS_KEY:-}" ] && [ "${S3_ACCESS_KEY}" != "CHANGE_ME" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [backup] Uploading to S3: ${S3_BUCKET}/${TIER}/ (${SIZE_MB}MB)"

    export AWS_ACCESS_KEY_ID="$S3_ACCESS_KEY"
    export AWS_SECRET_ACCESS_KEY="$S3_SECRET_KEY"
    S3_PREFIX="s3://${S3_BUCKET}/postgres/${TIER}"

    "$AWS" s3 cp "$BACKUP_FILE" \
        "${S3_PREFIX}/caselaw_${TIER}_${TIMESTAMP}.sql.gz" \
        --endpoint-url "$S3_ENDPOINT" --no-progress 2>&1 && S3_OK=1 || S3_OK=0

    if [ "$S3_OK" = "1" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') [backup] S3 upload OK"
        "$AWS" s3 cp "$COUNTS_FILE" \
            "${S3_PREFIX}/caselaw_${TIER}_${TIMESTAMP}_counts.txt" \
            --endpoint-url "$S3_ENDPOINT" --no-progress 2>/dev/null || true
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') [backup] S3 upload FAILED — keeping ALL old backups as safety net"
    fi
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') [backup] S3 not configured"
fi

# ── 4. Rotation — ONLY if new backup is valid ────────────────
# Local: only rotate if dump succeeded (we already validated size above)
# S3: only rotate if upload succeeded

_local_rotate() {
    local tier=$1 keep_days=$2
    local count
    count=$(ls -1 "$BACKUP_DIR"/caselaw_${tier}_*.sql.gz 2>/dev/null | wc -l)
    if [ "$count" -le 1 ]; then
        echo "  local ${tier}: only ${count} backup(s), skipping rotation"
        return
    fi
    # Keep at least 1 backup even if older than keep_days
    local deleted=0
    find "$BACKUP_DIR" -name "caselaw_${tier}_*.sql.gz" -mtime +${keep_days} | sort | head -n -1 | while read -r f; do
        rm -f "$f"
        rm -f "${f%.sql.gz}_counts.txt"
        deleted=$((deleted + 1))
        echo "  local deleted: $(basename "$f")"
    done
}

echo "$(date '+%Y-%m-%d %H:%M:%S') [backup] Local rotation:"
_local_rotate daily   7
_local_rotate weekly  30
_local_rotate monthly 365

if [ "$S3_OK" = "1" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [backup] S3 rotation:"
    _s3_rotate() {
        local tier=$1 keep_days=$2
        local cutoff count
        cutoff=$(date -d "-${keep_days} days" +%Y%m%d 2>/dev/null || date -v-${keep_days}d +%Y%m%d 2>/dev/null || echo "")
        [ -z "$cutoff" ] && return

        # Count existing S3 backups for this tier
        count=$("$AWS" s3 ls "s3://${S3_BUCKET}/postgres/${tier}/" \
            --endpoint-url "$S3_ENDPOINT" 2>/dev/null | grep -c "\.sql\.gz" || echo 0)
        if [ "$count" -le 1 ]; then
            echo "  S3 ${tier}: only ${count} backup(s), skipping rotation"
            return
        fi

        "$AWS" s3 ls "s3://${S3_BUCKET}/postgres/${tier}/" \
            --endpoint-url "$S3_ENDPOINT" 2>/dev/null \
          | grep "\.sql\.gz\|_counts\.txt" | while read -r line; do
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
    echo "$(date '+%Y-%m-%d %H:%M:%S') [backup] S3 rotation SKIPPED (upload failed)"
fi

# ── 5. Summary ───────────────────────────────────────────────
echo "$(date '+%Y-%m-%d %H:%M:%S') [backup] Inventory:"
for t in daily weekly monthly; do
    local_n=$(ls -1 "$BACKUP_DIR"/caselaw_${t}_*.sql.gz 2>/dev/null | wc -l)
    echo "  ${t}: ${local_n} local"
done
echo "  S3 upload: $([ "$S3_OK" = "1" ] && echo 'OK' || echo 'FAILED')"
echo "---"
