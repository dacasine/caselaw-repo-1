#!/bin/bash
set -euo pipefail
cd /srv/caselaw
export PYTHONPATH=/srv/caselaw
export OMP_NUM_THREADS=12
export MKL_NUM_THREADS=12

.venv/bin/python -c "
import torch, sys, os, json, time, sqlite3
torch.set_num_threads(12)
torch.set_num_interop_threads(2)

sys.path.insert(0, '/srv/caselaw')
from search_stack.parag.embedder import load_embedder, build_embedding_input, _vec_to_blob, fetch_pending_chunks, MAX_SEQ_LENGTH
import sqlite_vec

BATCH = 64
PROGRESS = 5000
DB_PATH = '/srv/data/parag_chunks.db'

# Open with long timeout + immediate retry on lock
conn = sqlite3.connect(DB_PATH, timeout=120.0, check_same_thread=False)
conn.execute('PRAGMA journal_mode=WAL')
conn.execute('PRAGMA busy_timeout=120000')
conn.enable_load_extension(True)
sqlite_vec.load(conn)
conn.enable_load_extension(False)

# Ensure vec_chunks exists
conn.execute('CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(embedding float[1024])')
conn.execute('''CREATE TABLE IF NOT EXISTS chunk_embeddings_meta (
    chunk_id INTEGER PRIMARY KEY, model TEXT NOT NULL,
    embedding_version INTEGER NOT NULL,
    encoded_at TEXT NOT NULL DEFAULT (datetime(\"now\")))''')
conn.commit()

print('loading model...', flush=True)
model = load_embedder(device='cpu')

rows = fetch_pending_chunks(conn, limit=None)
total = len(rows)
print(f'pending: {total}', flush=True)

done = 0
err = 0
t_start = time.monotonic()

for start in range(0, total, BATCH):
    batch = rows[start:start+BATCH]
    chunk_ids = [r[0] for r in batch]
    texts = [build_embedding_input(r[1], r[2]) for r in batch]

    vecs = model.encode(texts, batch_size=BATCH, show_progress_bar=False,
                        convert_to_numpy=True, normalize_embeddings=True)

    # Retry-resilient DB write
    for attempt in range(5):
        try:
            cur = conn.cursor()
            cur.executemany('DELETE FROM vec_chunks WHERE rowid = ?', [(c,) for c in chunk_ids])
            cur.executemany('INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)',
                           [(chunk_ids[i], _vec_to_blob(vecs[i])) for i in range(len(chunk_ids))])
            cur.executemany('''INSERT INTO chunk_embeddings_meta (chunk_id, model, embedding_version, encoded_at)
                VALUES (?, 'BAAI/bge-m3', 1, datetime('now'))
                ON CONFLICT(chunk_id) DO UPDATE SET
                    model='BAAI/bge-m3', embedding_version=1, encoded_at=datetime('now')''',
                           [(c,) for c in chunk_ids])
            conn.commit()
            break
        except sqlite3.OperationalError as e:
            if 'locked' in str(e) and attempt < 4:
                time.sleep(5 * (attempt + 1))
            else:
                err += len(batch)
                break

    done += len(batch)
    if done % PROGRESS == 0 or done >= total:
        elapsed = time.monotonic() - t_start
        rate = done / elapsed * 60
        eta = (total - done) / (done / elapsed) / 60
        print(f'  [{done}/{total}] rate={rate:.0f}/min err={err} eta={eta:.1f}min', flush=True)

print(json.dumps({'total': total, 'done': done, 'errors': err}), flush=True)
conn.close()
print('DONE', flush=True)
"
