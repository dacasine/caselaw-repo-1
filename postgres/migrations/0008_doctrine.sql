-- Doctrine nodes — structured legal context for RAG disambiguation.
-- ~300 nodes covering the full Swiss legal taxonomy.

CREATE TABLE IF NOT EXISTS doctrine_nodes (
    id          TEXT PRIMARY KEY,       -- hierarchical ID: "1.5.2.3"
    parent_id   TEXT,                   -- parent node: "1.5.2"
    level       INTEGER NOT NULL,       -- depth in tree (1=top, 4=leaf)
    title_fr    TEXT NOT NULL,
    title_de    TEXT,
    title_it    TEXT,
    content     TEXT NOT NULL,          -- full markdown body (500-2000 words)
    articles    TEXT[],                 -- ["CO 253-304", "OBLF"]
    sr_numbers  TEXT[],                 -- ["220", "221.213.11"]
    keywords_fr TEXT[],
    keywords_de TEXT[],
    keywords_it TEXT[],
    embedding   vector(1024),           -- BGE-M3 on content
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_doctrine_fts
    ON doctrine_nodes USING GIN (to_tsvector('simple', coalesce(title_fr,'') || ' ' || coalesce(title_de,'') || ' ' || coalesce(title_it,'') || ' ' || content));

CREATE INDEX IF NOT EXISTS idx_doctrine_keywords
    ON doctrine_nodes USING GIN (keywords_fr, keywords_de, keywords_it);

-- DiskANN vector index (created after data load for efficiency)
-- CREATE INDEX idx_doctrine_embedding ON doctrine_nodes
--     USING diskann (embedding vector_cosine_ops);
