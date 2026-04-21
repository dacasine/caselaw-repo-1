#!/bin/bash
set -euo pipefail
cd /srv/caselaw
export PYTHONPATH=/srv/caselaw
export OMP_NUM_THREADS=12
export MKL_NUM_THREADS=12
export TORCH_NUM_THREADS=12

.venv/bin/python -c "
import torch, sys, os, json
torch.set_num_threads(12)
torch.set_num_interop_threads(2)
print(f'threads: {torch.get_num_threads()}, interop: {torch.get_num_interop_threads()}', flush=True)

sys.path.insert(0, '/srv/caselaw')
from db_schema_parag import init_parag_schema
from search_stack.parag.embedder import encode_and_store, load_embedder

conn = init_parag_schema('/srv/data/parag_chunks.db')
model = load_embedder(device='cpu')
print('encoding batch=64...', flush=True)
stats = encode_and_store(conn, model, batch_size=64, progress_every=5000)
print(json.dumps(stats, indent=2, default=str), flush=True)
conn.close()
print('DONE', flush=True)
"
