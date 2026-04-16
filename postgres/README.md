# Hetzner deployment playbook

From local SQLite + sqlite-vec stack → Postgres + pgvectorscale on a
Hetzner AX102 (or equivalent). One command per step.

## Recommended machine

**Hetzner AX102** — Ryzen 9 7950X3D, 128 GB DDR5 ECC, 2 × 1.92 TB NVMe,
~€133/month. Spec justified in `docs/plan/` after sizing exercise.

## Step-by-step (from fresh Ubuntu 24.04 box)

### 1. Lock down the server (as root, first thing)

```bash
# On your laptop:
ssh root@<HETZNER_IP>

# Inside the server:
adduser damien --gecos ""
usermod -aG sudo damien
mkdir -p /home/damien/.ssh
cp ~/.ssh/authorized_keys /home/damien/.ssh/
chown -R damien:damien /home/damien/.ssh
chmod 700 /home/damien/.ssh
chmod 600 /home/damien/.ssh/authorized_keys

# Disable root SSH + password auth
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl reload ssh

apt update && apt upgrade -y
apt install -y ufw fail2ban git docker.io docker-compose-v2 curl jq
systemctl enable --now fail2ban
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
```

### 2. Add yourself to `~/.ssh/config` on your laptop

```
Host hetzner
  HostName <HETZNER_IP>
  User damien
  IdentityFile ~/.ssh/id_ed25519
  ServerAliveInterval 30
```

Test: `ssh hetzner 'id'` should return `uid=1000(damien)…`.

### 3. Clone the repo on the server

```bash
ssh hetzner
cd /opt
sudo git clone https://github.com/dacasine/caselaw-repo-1.git caselaw
sudo chown -R damien:damien /opt/caselaw
cd /opt/caselaw
git checkout parag/v1
```

### 4. Bring up Postgres (caselaw + doctrine)

```bash
cd /opt/caselaw/postgres/docker
cp .env.example .env
# Edit .env — set strong passwords and PUBLIC_HOST=your.domain.ch
nano .env

docker compose build postgres_caselaw
docker compose up -d postgres_caselaw postgres_doctrine

# Verify:
docker compose logs -f postgres_caselaw   # Ctrl-C after you see "database system is ready"
docker compose exec postgres_caselaw psql -U caselaw -d caselaw -c "\dx"
# Should list: vector, vectorscale, pg_trgm, unaccent, btree_gin
```

### 5. Transfer the SQLite source DBs from your Mac

```bash
# ON YOUR MAC:
# Paused pipeline if still running (or wait for weekend to finish).
# parag_chunks.db is ~5-10 GB depending on state.

rsync -av --progress \
  ~/.swiss-caselaw/parag_chunks.db \
  ~/.swiss-caselaw/statutes.db \
  ~/.swiss-caselaw/cantonal_laws.db \
  ~/.swiss-caselaw/reference_graph.db \
  hetzner:/srv/data/

# decisions.db is 55 GB — resumable transfer:
rsync -av --progress --partial \
  ~/.swiss-caselaw/decisions.db \
  hetzner:/srv/data/

# Or rebuild on server via HF (~30 min):
#   ssh hetzner 'cd /opt/caselaw && ./scripts/update_database_fresh.sh'
```

### 6. Run the migration

```bash
ssh hetzner
cd /opt/caselaw
python3 -m venv .venv
.venv/bin/pip install 'psycopg[binary,pool]>=3.1' sqlite-vec tqdm

# Export passwords to shell for the CLI
export PG_URL="postgres://caselaw:$(grep PG_CASELAW_PASSWORD postgres/docker/.env | cut -d= -f2)@localhost:5432/caselaw"

# Migration runs in order: decisions → chunks → enrichment → authority
# → vectors → citations → statutes → graph
.venv/bin/python -m postgres.migrate.sqlite_to_pg \
    --caselaw-url "$PG_URL" \
    --source-dir /srv/data
```

Expected duration: **2-4 h** on AX102 (most time in decisions + embeddings).
Idempotent: re-run after interruption picks up where it left off.

### 7. Front-end (LibreChat)

```bash
cd /opt/caselaw/postgres/docker
docker compose up -d librechat caddy
# Caddy gets Let's Encrypt cert automatically if DNS points here.
```

Open `https://<PUBLIC_HOST>` → you should see LibreChat.

### 8. Ingestion cron (daily updates)

```bash
sudo crontab -u damien -e
# Add:
0 2 * * *  cd /opt/caselaw && .venv/bin/python -m scripts.daily_ingest >> /var/log/caselaw/ingest.log 2>&1
```

### 9. Daily backup to Hetzner Storage Box (optional)

Order a BX40 (~€12/month) from Hetzner console, get SSH creds, then:

```bash
# Cron
0 4 * * *  /opt/caselaw/postgres/docker/backup_to_storagebox.sh
```

Script does `pg_dump --format=custom` on both DBs + rsync to Storage Box.

## Troubleshooting

### pgvectorscale extension missing
Our Dockerfile builds on `timescale/timescaledb-ha:pg16` which ships
it under the name `vectorscale`. If `CREATE EXTENSION vectorscale;`
fails, verify the image: `docker run --rm caselaw/postgres:16 sh -c 'ls /usr/share/postgresql/16/extension/ | grep -i scale'`.

### DiskANN index building slow on huge table
Build the DiskANN index *after* bulk loading (the migration script
doesn't create indexes upfront — migrations do). On 1M rows × 1024d
this takes ~15-20 min on AX102.

### SQLite → Postgres row count mismatch
Check the WAL snapshot: `ls -la /srv/data/parag_chunks.db-wal`. If
large, run a `PRAGMA wal_checkpoint(FULL);` on the source before
re-rsyncing.

## Rollback

The whole stack is a Docker compose — `docker compose down -v`
destroys it. The Mac source stays untouched. Worst case, you're out
one afternoon of setup.
