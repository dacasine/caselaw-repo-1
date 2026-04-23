"""BGE-M3 embedder + pgvector persistence for PA-RAG Phase 4.

Why BGE-M3 (not Longformer Swiss)
---------------------------------
Round 4 benchmark on real parag chunks showed the raw Swiss Longformer
checkpoint (MLM only, no sentence-pair fine-tuning) gives off-diagonal
cosine ~0.84 — no retrieval discrimination. BGE-M3 showed delta 0.22
between same-decision and different-decision pairs, correct thematic
retrieval on test queries, and is already what upstream uses.

What we encode
--------------
For each chunk we concatenate the SAC summary header (if any) with the
cleaned text and embed the whole as one vector. That way a query
matches both the contextual header and the raw content.

  embedding_input = (summary + "\\n\\n" + cleaned) if summary else cleaned

Storage
-------
- `chunk_embeddings` : Postgres table with a pgvector `vector(1024)`
                       column, keyed by chunks.id.
                       Tracks model + version per chunk, so model
                       upgrades can re-encode incrementally.
"""

from __future__ import annotations

import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=FutureWarning)

import gc
import os
import resource

import psycopg
import torch
from sentence_transformers import SentenceTransformer


def _rss_mb() -> float:
    """Current process resident-set size in megabytes."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)

from db_schema_parag import EMBEDDING_DIM, EMBEDDING_MODEL, EMBEDDING_VERSION


def pick_device() -> str:
    # CPU is the default because MPS unified-memory management with
    # transformers+long-sequence BGE-M3 can swap a Mac to its knees.
    # Users on a large Mac / Linux GPU can override via --device.
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


#: Max tokens passed to the encoder. BGE-M3's native max is 8192. Most of
#: our chunks are under 1500 tokens; capping at 1024 cuts activation memory
#: by ~8× and has negligible quality impact on our data.
MAX_SEQ_LENGTH = 1024


def load_embedder(model_name: str = EMBEDDING_MODEL, device: str | None = None) -> SentenceTransformer:
    device = device or pick_device()
    m = SentenceTransformer(model_name, device=device)
    m.max_seq_length = MAX_SEQ_LENGTH
    dim = m.get_sentence_embedding_dimension()
    if dim != EMBEDDING_DIM:
        raise RuntimeError(
            f"model {model_name} has dim={dim}, schema expects {EMBEDDING_DIM}"
        )
    return m


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def build_embedding_input(cleaned: str, summary: str | None) -> str:
    if summary:
        return f"{summary}\n\n{cleaned}"
    return cleaned


def _vec_to_text(v) -> str:
    """Convert a 1D float32 numpy array (or list) to pgvector text format."""
    if hasattr(v, "tolist"):
        v = v.tolist()
    return "[" + ",".join(str(x) for x in v) + "]"


def fetch_pending_chunks(
    conn: psycopg.Connection,
    *,
    limit: int | None = None,
    where_extra: str = "",
) -> list[tuple[int, str, str | None]]:
    """Return list of (chunk_id, cleaned, summary) for chunks that either
    have no vector yet OR whose vector is at an older EMBEDDING_VERSION.
    Skips stubs (summary_source='stub') — they have no useful content.

    Rows are ordered by content length so each batch contains chunks of
    similar size: this reduces padding waste AND keeps memory usage
    predictable (a long chunk won't be paired with a small one in a
    batch of mixed sizes).
    """
    q = (
        "SELECT c.id, c.cleaned, c.summary "
        "FROM chunks c "
        "LEFT JOIN chunk_embeddings ce ON ce.chunk_id = c.id "
        "WHERE (ce.chunk_id IS NULL OR ce.embedding_version < %s) "
        "  AND c.summary_source != 'stub' "
        "  AND length(c.cleaned) > 50 "
    )
    args: list = [EMBEDDING_VERSION]
    if where_extra:
        q += f"AND ({where_extra}) "
    q += "ORDER BY length(c.cleaned), c.id"
    if limit:
        q += " LIMIT %s"
        args.append(limit)
    with conn.cursor() as cur:
        cur.execute(q, args)
        return cur.fetchall()


def upsert_vectors(
    conn: psycopg.Connection,
    chunk_ids: list[int],
    vectors,
) -> None:
    """Insert (or update) the embedding for each chunk_id."""
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO chunk_embeddings (chunk_id, embedding, model, embedding_version, encoded_at)
            VALUES (%s, %s::vector, %s, %s, now())
            ON CONFLICT (chunk_id) DO UPDATE SET
                embedding = EXCLUDED.embedding,
                model = EXCLUDED.model,
                embedding_version = EXCLUDED.embedding_version,
                encoded_at = now()
            """,
            [
                (cid, _vec_to_text(vectors[i]), EMBEDDING_MODEL, EMBEDDING_VERSION)
                for i, cid in enumerate(chunk_ids)
            ],
        )


# ---------------------------------------------------------------------------
# Batch encoder
# ---------------------------------------------------------------------------

def encode_and_store(
    conn: psycopg.Connection,
    model: SentenceTransformer,
    batch_size: int = 16,
    progress_every: int = 500,
    limit: int | None = None,
    where_extra: str = "",
) -> dict:
    """Encode all pending chunks and store vectors. Commits every batch.

    Returns a stats dict.
    """
    rows = fetch_pending_chunks(conn, limit=limit, where_extra=where_extra)
    total = len(rows)
    stats = {"total": total, "encoded": 0, "errors": 0,
             "elapsed_s": 0.0, "enc_s": 0.0, "db_s": 0.0}
    if not rows:
        return stats

    t_start = time.monotonic()
    done = 0
    for start in range(0, total, batch_size):
        batch = rows[start:start + batch_size]
        chunk_ids = [r[0] for r in batch]
        texts = [build_embedding_input(r[1], r[2]) for r in batch]

        t0 = time.monotonic()
        try:
            vecs = model.encode(
                texts,
                batch_size=batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
                normalize_embeddings=True,
            )
        except Exception as exc:
            stats["errors"] += len(batch)
            done += len(batch)
            continue
        stats["enc_s"] += time.monotonic() - t0

        t0 = time.monotonic()
        upsert_vectors(conn, chunk_ids, vecs)
        conn.commit()
        stats["db_s"] += time.monotonic() - t0

        stats["encoded"] += len(batch)
        done += len(batch)

        # Help the allocator every few batches — transformers/torch tend
        # to hold on to large scratch buffers otherwise.
        if done % (batch_size * 4) == 0:
            del vecs
            gc.collect()

        if done % progress_every == 0 or done == total:
            elapsed = time.monotonic() - t_start
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            print(
                f"  [{done}/{total}] encoded={stats['encoded']} "
                f"err={stats['errors']} | "
                f"rate={rate*60:.0f}/min "
                f"enc={stats['enc_s']/done*1000:.0f}ms "
                f"db={stats['db_s']/done*1000:.0f}ms "
                f"| rss={_rss_mb():.0f}MB eta={eta/60:.1f}min",
                flush=True,
            )

    stats["elapsed_s"] = time.monotonic() - t_start
    return stats
