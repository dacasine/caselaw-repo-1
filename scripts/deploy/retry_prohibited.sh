#!/bin/bash
# Retry PROHIBITED_CONTENT errors via synthetic.new / Kimi-K2-Instruct
# (no safety filter, handles sensitive legal content)
set -uo pipefail
cd /srv/caselaw
export PYTHONPATH=/srv/caselaw

.venv/bin/python -c "
import sqlite3, json, sys, os, time
sys.path.insert(0, '/srv/caselaw')

from search_stack.parag.llm_client import SyntheticClient
from search_stack.parag.enrichment import (
    SYSTEM_PROMPT_FULL, DecisionContext, build_user_prompt,
    canonicalise_bge, validate_full, ENRICHMENT_PROMPT_VERSION
)
from search_stack.parag.persistence import source_hash

parag = sqlite3.connect('/srv/data/parag_chunks.db', timeout=120, check_same_thread=False)
src = sqlite3.connect('/srv/data/decisions.db', timeout=30)
src.row_factory = sqlite3.Row

# Find all PROHIBITED errors
prohibited = parag.execute(
    \"SELECT decision_id FROM decision_enrichment WHERE error_message LIKE '%PROHIBITED%'\"
).fetchall()
dids = [r[0] for r in prohibited]
print(f'{len(dids)} PROHIBITED decisions to retry via Kimi', flush=True)

if not dids:
    sys.exit(0)

client = SyntheticClient(
    model='hf:meta-llama/Llama-3.3-70B-Instruct',
    rate_limit_per_minute=20,
    quota_aware=True,
)

ok = err = 0
for i, did in enumerate(dids):
    row = src.execute(
        'SELECT court, language, decision_date, chamber, COALESCE(regeste,\"\") AS regeste, full_text FROM decisions WHERE decision_id=?',
        (did,)
    ).fetchone()
    if not row:
        continue

    # Build context
    headers = [r[0] for r in parag.execute(
        'SELECT summary FROM chunks WHERE decision_id=? AND summary IS NOT NULL ORDER BY span_start', (did,)
    ).fetchall()]
    disp = parag.execute('SELECT cleaned FROM chunks WHERE decision_id=? ORDER BY span_start DESC LIMIT 1', (did,)).fetchone()

    ctx = DecisionContext(
        decision_id=did, court=row['court'], language=row['language'],
        date_iso=row['decision_date'] or '', chamber=row['chamber'],
        regeste=row['regeste'], full_text=row['full_text'],
        chunk_headers=headers, dispositif_text=disp[0] if disp else None
    )
    user = build_user_prompt(ctx)

    try:
        resp = client.chat(system=SYSTEM_PROMPT_FULL, user=user, max_tokens=3500, temperature=0.1)
    except Exception as e:
        err += 1
        continue

    raw = resp.content.strip()
    if raw.startswith('\`\`\`'):
        raw = raw.split('\`\`\`', 2)[1]
        if raw.startswith('json'): raw = raw[4:]

    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        err += 1
        continue

    obj = canonicalise_bge(obj)
    errors = validate_full(obj)
    sort = obj.get('sort', {})
    status = 'ok' if not errors else 'schema_invalid'

    parag.execute('''
        UPDATE decision_enrichment SET
            procedural_stage=?, outcome=?, subject_matter=?,
            principle_questions=?, obiter_dicta=?, doctrine_discussion=?,
            llm_latency_s=?, prompt_tokens=?, completion_tokens=?,
            status=?, error_message=NULL, processed_at=datetime('now')
        WHERE decision_id=?
    ''', (
        sort.get('procedural_stage'), sort.get('outcome'), sort.get('subject_matter'),
        json.dumps(obj.get('principle_questions') or [], ensure_ascii=False),
        json.dumps(obj.get('obiter_dicta') or [], ensure_ascii=False),
        json.dumps(obj.get('doctrine_discussion') or {}, ensure_ascii=False),
        resp.latency_s, resp.usage.get('prompt_tokens', 0), resp.usage.get('completion_tokens', 0),
        status, did
    ))
    if i % 10 == 0:
        parag.commit()
    ok += 1

    if (i + 1) % 50 == 0:
        print(f'  [{i+1}/{len(dids)}] ok={ok} err={err}', flush=True)

parag.commit()
print(f'DONE: {ok} ok, {err} errors out of {len(dids)}', flush=True)
parag.close()
src.close()
"
