-- Per-decision authority scoring (court_level, PageRank, validity).

CREATE TABLE IF NOT EXISTS decision_authority (
    decision_id       TEXT PRIMARY KEY,
    court_level       INTEGER,
    atf_published     BOOLEAN NOT NULL DEFAULT FALSE,
    authority_score   REAL,
    pagerank_raw      REAL,
    pagerank_temporal REAL,
    validity_status   TEXT,              -- valid | distinguished | criticized | overruled
    n_overruled_by    INTEGER NOT NULL DEFAULT 0,
    n_criticized_by   INTEGER NOT NULL DEFAULT 0,
    n_confirmed_by    INTEGER NOT NULL DEFAULT 0,
    n_cited_by        INTEGER NOT NULL DEFAULT 0,
    computed_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dauth_level ON decision_authority (court_level);
CREATE INDEX IF NOT EXISTS idx_dauth_valid ON decision_authority (validity_status);
CREATE INDEX IF NOT EXISTS idx_dauth_score ON decision_authority (authority_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_dauth_prank ON decision_authority (pagerank_temporal DESC NULLS LAST);

-- Per-decision enrichment state (idempotent re-run tracker — mirrors SQLite).
CREATE TABLE IF NOT EXISTS enrichment_state (
    decision_id       TEXT PRIMARY KEY,
    court             TEXT NOT NULL,
    parser_name       TEXT,
    fallback_used     INTEGER DEFAULT 0,
    n_chunks          INTEGER,
    n_stubs           INTEGER,
    n_self_suff       INTEGER,
    n_summarized      INTEGER,
    n_errors          INTEGER,
    llm_calls         INTEGER,
    llm_latency_s     REAL,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    source_hash       TEXT,
    prompt_version    INTEGER NOT NULL DEFAULT 1,
    status            TEXT NOT NULL,
    error_message     TEXT,
    processed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_estate_court  ON enrichment_state (court);
CREATE INDEX IF NOT EXISTS idx_estate_status ON enrichment_state (status);
