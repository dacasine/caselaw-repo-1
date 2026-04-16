-- Decisions table — mirrors upstream's db_schema.py `decisions`.
-- 965k rows, ~55 GB raw text → ~70-80 GB in Postgres with indexes.
-- Partitioned by year to keep indexes manageable.

CREATE TABLE IF NOT EXISTS decisions (
    decision_id        TEXT        NOT NULL,
    court              TEXT        NOT NULL,
    canton             TEXT        NOT NULL,
    chamber            TEXT,
    docket_number      TEXT        NOT NULL,
    docket_number_2    TEXT,
    decision_date      DATE,
    publication_date   DATE,
    language           TEXT        NOT NULL,
    title              TEXT,
    legal_area         TEXT,
    regeste            TEXT,
    abstract_de        TEXT,
    abstract_fr        TEXT,
    abstract_it        TEXT,
    full_text          TEXT,
    decision_type      TEXT,
    outcome            TEXT,
    source_url         TEXT,
    pdf_url            TEXT,
    cited_decisions    TEXT,
    scraped_at         TIMESTAMPTZ,
    source             TEXT,
    source_id          TEXT,
    source_spider      TEXT,
    content_hash       TEXT,
    json_data          JSONB,
    canonical_key      TEXT,
    decision_year      INTEGER     GENERATED ALWAYS AS (
        COALESCE(EXTRACT(YEAR FROM decision_date)::INTEGER, 0)
    ) STORED,

    -- Full-text search column, trilingual, weighted by importance.
    -- Uses simple tokenizer to avoid language-dependent stemming mistakes
    -- on legal abbreviations (art. 42 LTF etc.).
    fts tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', unaccent(COALESCE(title,    ''))), 'A') ||
        setweight(to_tsvector('simple', unaccent(COALESCE(regeste,  ''))), 'B') ||
        setweight(to_tsvector('simple', unaccent(COALESCE(full_text,''))), 'C')
    ) STORED,

    PRIMARY KEY (decision_id, decision_year)
);

-- Indexes on the parent table (inherited by partitions)
CREATE INDEX IF NOT EXISTS idx_decisions_court       ON decisions (court);
CREATE INDEX IF NOT EXISTS idx_decisions_canton      ON decisions (canton);
CREATE INDEX IF NOT EXISTS idx_decisions_date        ON decisions (decision_date);
CREATE INDEX IF NOT EXISTS idx_decisions_language    ON decisions (language);
CREATE INDEX IF NOT EXISTS idx_decisions_docket      ON decisions (docket_number);
CREATE INDEX IF NOT EXISTS idx_decisions_canonical   ON decisions (canonical_key);
CREATE INDEX IF NOT EXISTS idx_decisions_fts         ON decisions USING GIN (fts);
