# Deployment Guide — Swiss PA-RAG on a fresh server

## Prerequisites

- **OS** : Ubuntu 24.04 LTS (Server)
- **Hardware** : 12+ cores, 64+ GB RAM, 500+ GB NVMe
- **SSH** : public key deployed, alias `ovh` (or your choice) in `~/.ssh/config`
- **Domain** : A record pointing to the server IP (optional, for HTTPS)
- **API keys** : OpenRouter (gemini-2.0-flash), optionally synthetic.new (Kimi fallback)

## Step 1 — Harden the server (5 min)

```bash
ssh root@<IP>

# Create sudo user
adduser damien --gecos ""
usermod -aG sudo damien
mkdir -p /home/damien/.ssh
cp ~/.ssh/authorized_keys /home/damien/.ssh/
chown -R damien:damien /home/damien/.ssh

# Lock down SSH
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl reload ssh

# Essentials
apt update && apt upgrade -y
apt install -y ufw fail2ban docker.io docker-compose-v2 git curl jq sqlite3 tmux htop

# Firewall
ufw default deny incoming && ufw default allow outgoing
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp
ufw --force enable

# Docker for current user
usermod -aG docker damien
systemctl enable --now docker fail2ban
```

## Step 2 — Clone repo + install deps (5 min)

```bash
ssh ovh  # as your sudo user

# Clone
sudo mkdir -p /srv/caselaw /srv/data /srv/data/logs
sudo chown -R $(whoami):$(whoami) /srv/caselaw /srv/data
cd /srv/caselaw
git clone https://github.com/dacasine/caselaw-repo-1.git .
git checkout parag/phase-3-sac

# Python venv
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install \
    sentence-transformers sqlite-vec 'psycopg[binary,pool]>=3.1' tqdm \
    requests beautifulsoup4 lxml playwright pymupdf pydantic \
    huggingface-hub pyarrow httpx aiohttp onnxruntime optimum

# Playwright browser (for BGer/BGE Incapsula bypass)
.venv/bin/playwright install chromium
sudo .venv/bin/playwright install-deps || true
```

## Step 3 — Transfer data from source (30-90 min)

From your Mac (or any machine that has the SQLite DBs):

```bash
# Transfer all DBs (decisions.db is 55 GB — longest transfer)
rsync -avz --progress --partial \
    ~/.swiss-caselaw/decisions.db \
    ~/.swiss-caselaw/parag_chunks.db \
    ~/.swiss-caselaw/statutes.db \
    ~/.swiss-caselaw/cantonal_laws.db \
    ~/.swiss-caselaw/reference_graph.db \
    ovh:/srv/data/

# IMPORTANT: checkpoint WAL before transfer if source is actively writing:
#   sqlite3 ~/.swiss-caselaw/parag_chunks.db "PRAGMA wal_checkpoint(FULL);"
```

## Step 4 — Symlink DBs to default path (1 min)

```bash
ssh ovh 'mkdir -p ~/.swiss-caselaw
for db in decisions.db parag_chunks.db statutes.db cantonal_laws.db reference_graph.db; do
    ln -sf /srv/data/$db ~/.swiss-caselaw/$db
done'
```

This lets all scripts (which default to `~/.swiss-caselaw/`) find the data.

## Step 5 — Deploy API keys (1 min)

```bash
ssh ovh 'cat > /srv/caselaw/.env << EOF
OPENROUTER_API_KEY=sk-or-v1-...your-key...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=google/gemini-2.0-flash-001
OPENROUTER_FALLBACK_MODEL=google/gemini-2.0-flash-001

SYNTHETIC_API_KEY=syn_...your-key...
SYNTHETIC_BASE_URL=https://api.synthetic.new/v1
SYNTHETIC_MODEL=hf:meta-llama/Llama-3.3-70B-Instruct
EOF
chmod 600 /srv/caselaw/.env'
```

**IMPORTANT** : fallback = même modèle que primary (pas un modèle plus cher).

## Step 6 — Seed scraper state files (5 min)

Prevents scrapers from re-downloading all 965k existing decisions:

```bash
ssh ovh 'cd /srv/caselaw && PYTHONPATH=/srv/caselaw .venv/bin/python -c "
import sqlite3, json, os
src = sqlite3.connect(\"/srv/data/decisions.db\", timeout=30)
courts = [r[0] for r in src.execute(\"SELECT DISTINCT court FROM decisions\").fetchall()]
os.makedirs(\"state\", exist_ok=True)
for court in courts:
    sf = f\"state/{court}.jsonl\"
    if os.path.exists(sf): continue
    dids = [r[0] for r in src.execute(\"SELECT decision_id FROM decisions WHERE court=?\", (court,)).fetchall()]
    if not dids: continue
    with open(sf, \"w\") as f:
        for did in dids:
            f.write(json.dumps({\"decision_id\": did}) + chr(10))
    print(f\"  {court}: {len(dids)} IDs seeded\")
src.close()
"'
```

## Step 7 — Compute authority scores (30 sec)

```bash
ssh ovh 'cd /srv/caselaw && PYTHONPATH=/srv/caselaw \
    .venv/bin/python scripts/parag/compute_authority.py \
    --parag-db /srv/data/parag_chunks.db'
```

## Step 8 — Install cron jobs (1 min)

```bash
ssh ovh 'crontab -l 2>/dev/null
(crontab -l 2>/dev/null; cat << CRON
# Daily scrape + publish + SAC + Phase5 + embed + authority
0 2 * * * /srv/caselaw/scripts/deploy/daily_pipeline.sh >> /srv/data/logs/daily_pipeline.log 2>&1
# Weekly entscheidsuche.ch gap-fill
0 0 * * 0 /srv/caselaw/scripts/deploy/weekly_entscheidsuche.sh >> /srv/data/logs/entscheidsuche.log 2>&1
CRON
) | sort -u | crontab -
echo "Cron installed:"
crontab -l'
```

## Step 9 — Test the RAG (1 min)

```bash
# Semantic search
ssh ovh 'bash /srv/caselaw/scripts/deploy/search_ovh.sh \
    "interruption de la prescription dans la poursuite pour dettes"'

# Full status dashboard
ssh ovh 'bash /srv/caselaw/scripts/deploy/status.sh'
```

## Step 10 — Optional: run the full PA-RAG pipeline

If `parag_chunks.db` was transferred with existing enrichments, you're done.
If starting from scratch (only `decisions.db`):

```bash
# SAC + Phase 5 for all courts (can run for days)
ssh ovh 'nohup bash /srv/caselaw/scripts/deploy/run_sac_phase5_ovh.sh \
    > /srv/data/logs/sac_phase5.log 2>&1 &'

# Embedding (CPU-intensive, run separately to avoid SQLite lock contention)
ssh ovh 'nohup bash /srv/caselaw/scripts/deploy/run_embed_resilient.sh \
    > /srv/data/logs/embed.log 2>&1 &'
```

## Monitoring

```bash
# Dashboard
ssh ovh 'bash /srv/caselaw/scripts/deploy/status.sh'

# Scraper freshness
ssh ovh 'cd /srv/caselaw && .venv/bin/python scripts/check_scraper_freshness.py'

# Daily pipeline log
ssh ovh 'tail -30 /srv/data/logs/daily_pipeline.log'

# Embed progress
ssh ovh 'tail -5 /srv/data/logs/embed.log'
```

## File layout on server

```
/srv/caselaw/                    ← git repo (parag/phase-3-sac branch)
    .env                         ← API keys (never committed)
    .venv/                       ← Python virtual environment
    output/decisions/*.jsonl     ← scraper output (per court)
    state/*.jsonl                ← scraper state (known decision IDs)
    scripts/deploy/              ← all operational scripts

/srv/data/                       ← persistent data (outside repo)
    decisions.db                 ← source decisions (55 GB)
    parag_chunks.db              ← PA-RAG enriched chunks + vectors
    statutes.db                  ← federal laws (Fedlex)
    cantonal_laws.db             ← cantonal laws (LexFind)
    reference_graph.db           ← citation graph (8.84M edges)
    logs/                        ← all pipeline logs
    entscheidsuche/              ← bulk archive downloads (weekly)

~/.swiss-caselaw/                ← symlinks → /srv/data/*.db
```

## Troubleshooting

### "database is locked"
SQLite single-writer limitation. Never run embed + Phase5 simultaneously.
Use `run_embed_resilient.sh` which retries with 120s timeout.

### PROHIBITED_CONTENT errors (Gemini)
Google safety filter blocks some criminal/sensitive decisions.
Run `retry_prohibited.sh` which uses Llama 3.3 via synthetic.new (no filter).

### Scraper re-downloads everything
State files missing. Run the seed script from Step 6.

### OpenRouter 504s
Google backend unstable. The fallback should NOT be a more expensive model.
Set `OPENROUTER_FALLBACK_MODEL=google/gemini-2.0-flash-001` (same as primary).

### How to update the code
```bash
# On your Mac (development):
git push origin parag/phase-3-sac

# On OVH (deployment):
ssh ovh 'cd /srv/caselaw && git pull origin parag/phase-3-sac'
```
Never commit directly on OVH.
