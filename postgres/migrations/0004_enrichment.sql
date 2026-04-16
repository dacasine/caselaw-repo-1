-- Phase 5 per-decision enrichment + authority scoring.
-- Mirrors parag_chunks.db decision_enrichment / decision_authority tables.

CREATE TABLE IF NOT EXISTS decision_enrichment (
    decision_id         TEXT PRIMARY KEY,
    court               TEXT NOT NULL,
    procedural_stage    TEXT,        -- 'recours' | 'premiere_instance'
    outcome             TEXT,        -- 'admission' | 'admission_partielle' | 'rejet' | 'irrecevabilite'
    subject_matter      TEXT,
    principle_questions JSONB,       -- [{question, ratio, legal_basis[]}]
    obiter_dicta        JSONB,       -- [string]
    doctrine_discussion JSONB,       -- {discussed, authors_cited[], positions_weighed[], is_leading_case_signal}
    language_detected   TEXT,
    source_hash         TEXT,
    prompt_version      INTEGER NOT NULL DEFAULT 1,
    llm_latency_s       REAL,
    prompt_tokens       INTEGER,
    completion_tokens   INTEGER,
    status              TEXT NOT NULL,     -- 'ok' | 'error' | 'parse_fail' | 'schema_invalid'
    error_message       TEXT,
    processed_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_denrich_outcome ON decision_enrichment (outcome);
CREATE INDEX IF NOT EXISTS idx_denrich_status  ON decision_enrichment (status);
CREATE INDEX IF NOT EXISTS idx_denrich_court   ON decision_enrichment (court);

-- JSONB GIN indexes for structured queries
CREATE INDEX IF NOT EXISTS idx_denrich_pqs_gin
    ON decision_enrichment USING GIN (principle_questions);
CREATE INDEX IF NOT EXISTS idx_denrich_doctrine_gin
    ON decision_enrichment USING GIN (doctrine_discussion);


-- ──────────────────────────────────────────────────────────────────────
-- Per-chunk citation resolution (law + case)
-- ──────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS chunk_law_citations (
    id           BIGSERIAL PRIMARY KEY,
    chunk_id     BIGINT NOT NULL,
    sr_number    TEXT,
    law_abbr     TEXT NOT NULL,
    article_num  TEXT,
    paragraph    TEXT,
    letter       TEXT,
    raw_text     TEXT NOT NULL,
    normalized   TEXT NOT NULL,
    source       TEXT NOT NULL,       -- 'llm' | 'regex' | 'both'
    resolved     BOOLEAN NOT NULL DEFAULT FALSE,
    FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_clc_chunk       ON chunk_law_citations (chunk_id);
CREATE INDEX IF NOT EXISTS idx_clc_law_article ON chunk_law_citations (law_abbr, article_num);
CREATE INDEX IF NOT EXISTS idx_clc_sr_article  ON chunk_law_citations (sr_number, article_num);
CREATE INDEX IF NOT EXISTS idx_clc_normalized  ON chunk_law_citations (normalized);
CREATE INDEX IF NOT EXISTS idx_clc_resolved    ON chunk_law_citations (resolved);


CREATE TABLE IF NOT EXISTS chunk_case_citations (
    id                  BIGSERIAL PRIMARY KEY,
    chunk_id            BIGINT NOT NULL,
    target_decision_id  TEXT NOT NULL,
    citation_type       TEXT NOT NULL,    -- 'bge' | 'docket'
    raw_text            TEXT NOT NULL,
    source              TEXT NOT NULL,
    direction           TEXT,              -- confirms | develops | distinguishes | overrules | criticizes | neutral
    FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ccc_chunk     ON chunk_case_citations (chunk_id);
CREATE INDEX IF NOT EXISTS idx_ccc_target    ON chunk_case_citations (target_decision_id);
CREATE INDEX IF NOT EXISTS idx_ccc_direction ON chunk_case_citations (direction);
