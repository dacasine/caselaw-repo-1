#!/bin/bash
# Quick semantic search on OVH
# Usage: bash /srv/caselaw/search_ovh.sh "interruption de la prescription LP"
#        bash /srv/caselaw/search_ovh.sh "Verjährung SchKG" --k 15 --language de

cd /srv/caselaw
export PYTHONPATH=/srv/caselaw
QUERY="$1"; shift
.venv/bin/python -c "
import sys, struct, warnings, sqlite3
warnings.filterwarnings('ignore')
sys.path.insert(0, '/srv/caselaw')

import sqlite_vec
from search_stack.parag.embedder import load_embedder

query = '''$QUERY'''
args = dict(k=10, language=None)
# parse remaining args
import argparse
ap = argparse.ArgumentParser()
ap.add_argument('--k', type=int, default=10)
ap.add_argument('--language', default=None)
ap.add_argument('--court', default=None)
parsed = ap.parse_args('$@'.split() if '$@' else [])
args.update(vars(parsed))

print('loading BGE-M3...', file=sys.stderr)
model = load_embedder(device='cpu')
qvec = model.encode([query], convert_to_numpy=True, normalize_embeddings=True, show_progress_bar=False)[0]

conn = sqlite3.connect('/srv/data/parag_chunks.db', timeout=30)
conn.enable_load_extension(True)
sqlite_vec.load(conn)
conn.enable_load_extension(False)

blob = struct.pack(f'{len(qvec)}f', *qvec.tolist())

filters = ''
params = [blob, args['k']]
if args['language']:
    filters += ' AND c.language = ?'
    params.append(args['language'])
if args['court']:
    filters += ' AND c.court = ?'
    params.append(args['court'])

rows = conn.execute(f'''
    SELECT v.rowid, v.distance,
           c.decision_id, c.court, c.language, c.considerant_number,
           c.summary, substr(c.cleaned, 1, 300),
           COALESCE(a.court_level, 2) AS court_level,
           COALESCE(a.atf_published, 0) AS atf,
           COALESCE(a.validity_status, \"valid\") AS validity
    FROM vec_chunks v
    JOIN chunks c ON c.id = v.rowid
    LEFT JOIN decision_authority a ON a.decision_id = c.decision_id
    WHERE v.embedding MATCH ? AND k = ? {filters}
    ORDER BY v.distance
''', params).fetchall()

print(f'\\n=== QUERY: {query!r} ===')
print(f'    k={args[\"k\"]} language={args[\"language\"]} court={args[\"court\"]}')
print(f'    {len(rows)} results\\n')

for i, r in enumerate(rows, 1):
    rid, dist, did, court, lang, num, summary, text, level, atf, validity = r
    cos = 1 - dist / 2
    badges = []
    if atf: badges.append('ATF')
    badges.append(f'L{level}')
    if validity != 'valid': badges.append(validity.upper())
    badge = ' '.join(f'[{b}]' for b in badges)

    print(f'-- {i:>2}  cos={cos:.3f}  {did}  {badge}  [{lang}] cons {num}')
    if summary:
        print(f'       > {summary}')
    print(f'       {\" \".join(text.split())[:250]}...')
    print()
" "$@"
