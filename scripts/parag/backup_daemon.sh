#!/usr/bin/env bash
# Hourly incremental snapshot of PA-RAG state. Retains 3 most recent hours.
#
# Backs up every DB that matters except decisions.db (55 GB, re-derivable
# from the HuggingFace parquet source via `update_database`).
#
# Uses SQLite's online .backup API — safe while the target DB is being
# written to (WAL mode handles it). Falls back to cp for non-SQLite files
# we might add later.
#
# Usage:
#     nohup bash scripts/parag/backup_daemon.sh > logs/backup.log 2>&1 &
#     echo $! > logs/backup.pid

set -u

DATA_DIR="${HOME}/.swiss-caselaw"
BACKUP_DIR="${DATA_DIR}/backups"
RETENTION_MIN=180   # 3 hours
INTERVAL_SEC=3600   # 1 hour

DBS=(parag_chunks.db statutes.db cantonal_laws.db reference_graph.db materialien.db)

mkdir -p "${BACKUP_DIR}"
HISTORY_LOG="${BACKUP_DIR}/history.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "${HISTORY_LOG}"
}

snapshot() {
    local ts dir src dst bytes total=0
    ts=$(date +%Y%m%d_%H%M%S)
    dir="${BACKUP_DIR}/${ts}"
    mkdir -p "${dir}"

    for db in "${DBS[@]}"; do
        src="${DATA_DIR}/${db}"
        dst="${dir}/${db}"
        if [[ ! -s "${src}" ]]; then
            continue  # skip empty or missing
        fi
        # Online backup via sqlite3 — consistent even during heavy writes.
        if sqlite3 "${src}" ".backup '${dst}'" 2>>"${HISTORY_LOG}"; then
            bytes=$(stat -f '%z' "${dst}" 2>/dev/null || echo 0)
            total=$((total + bytes))
        else
            log "sqlite3 .backup failed for ${db}, falling back to cp"
            cp "${src}" "${dst}" && bytes=$(stat -f '%z' "${dst}" 2>/dev/null || echo 0) && total=$((total + bytes))
        fi
    done

    # Compute human-readable size of this snapshot
    local sz
    sz=$(du -sh "${dir}" 2>/dev/null | awk '{print $1}')
    log "snapshot ${ts} written (${sz})"
}

prune() {
    local removed=0
    # Remove timestamped subdirs older than retention window.
    while IFS= read -r d; do
        rm -rf "${d}" && removed=$((removed + 1))
    done < <(find "${BACKUP_DIR}" -mindepth 1 -maxdepth 1 -type d -mmin +"${RETENTION_MIN}" 2>/dev/null)
    if [[ ${removed} -gt 0 ]]; then
        log "pruned ${removed} snapshot(s) older than ${RETENTION_MIN} min"
    fi
}

log "backup daemon started  DATA_DIR=${DATA_DIR} RETENTION_MIN=${RETENTION_MIN} INTERVAL_SEC=${INTERVAL_SEC}"

# Immediate snapshot, then hourly loop.
snapshot
prune

while true; do
    sleep "${INTERVAL_SEC}"
    snapshot
    prune
done
