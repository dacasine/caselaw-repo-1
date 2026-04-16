-- Phase 3 SAC chunks + Phase 4 embeddings.
-- 928k+ rows after the weekend pipeline completes.

CREATE TABLE IF NOT EXISTS chunks (
    id                  BIGSERIAL PRIMARY KEY,
    legacy_id           BIGINT UNIQUE,            -- preserves SQLite chunks.id for stable references
    decision_id         TEXT    NOT NULL,
    court               TEXT    NOT NULL,
    language            TEXT    NOT NULL,
    considerant_number  TEXT,                     -- "3" / "3.1" / "3.1.2" / "implicit"
    depth               INTEGER,
    span_start          INTEGER NOT NULL,
    span_end            INTEGER NOT NULL,
    raw_length          INTEGER,
    cleaned             TEXT    NOT NULL,
    summary             TEXT,                     -- SAC summary header (Gemini-generated)
    summary_source      TEXT,                     -- 'stub' | 'self_sufficient' | 'llm' | 'error'
    chunk_hash          TEXT,
    prompt_version      INTEGER NOT NULL DEFAULT 1,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    fts tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', unaccent(COALESCE(summary, ''))), 'A') ||
        setweight(to_tsvector('simple', unaccent(COALESCE(cleaned, ''))), 'B')
    ) STORED,

    UNIQUE (decision_id, considerant_number, span_start)
);

CREATE INDEX IF NOT EXISTS idx_chunks_decision       ON chunks (decision_id);
CREATE INDEX IF NOT EXISTS idx_chunks_court          ON chunks (court);
CREATE INDEX IF NOT EXISTS idx_chunks_summary_source ON chunks (summary_source);
CREATE INDEX IF NOT EXISTS idx_chunks_prompt_version ON chunks (prompt_version);
CREATE INDEX IF NOT EXISTS idx_chunks_fts            ON chunks USING GIN (fts);

-- ──────────────────────────────────────────────────────────────────────
-- Vector embeddings (BGE-M3 1024-dim, pgvectorscale DiskANN).
-- Separate table to keep the hot chunks row compact and to make
-- re-embedding (model bump) a pure UPDATE on this table.
-- ──────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS chunk_embeddings (
    chunk_id           BIGINT  PRIMARY KEY,
    embedding          vector(1024) NOT NULL,
    model              TEXT    NOT NULL,
    embedding_version  INTEGER NOT NULL,
    encoded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
);

-- StreamingDiskANN index (pgvectorscale). Parameters tuned from the
-- Phase 4 sub-plan. num_neighbors=50 gives ~98% recall@10 vs brute
-- force on our chunk distribution; search_list_size=100 at query time
-- trades ~2× latency for the recall floor.
CREATE INDEX IF NOT EXISTS idx_chunk_emb_diskann
    ON chunk_embeddings
    USING diskann (embedding vector_cosine_ops)
    WITH (num_neighbors = 50, search_list_size = 100);

CREATE INDEX IF NOT EXISTS idx_chunk_emb_model_ver
    ON chunk_embeddings (model, embedding_version);
