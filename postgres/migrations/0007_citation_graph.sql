-- Citation graph — mirrors reference_graph.db.
-- 8.84M decision→decision edges + 11.34M decision→statute edges.

CREATE TABLE IF NOT EXISTS decision_citations (
    source_decision_id  TEXT NOT NULL,
    target_decision_id  TEXT NOT NULL,
    raw_text            TEXT,
    confidence_score    REAL,
    resolution_method   TEXT,            -- 'docket_pattern' | 'court_inference' | 'bge_direct' ...
    citing_date         DATE,            -- denormalised from decisions.decision_date
    PRIMARY KEY (source_decision_id, target_decision_id)
);

CREATE INDEX IF NOT EXISTS idx_deccit_target ON decision_citations (target_decision_id);
CREATE INDEX IF NOT EXISTS idx_deccit_source ON decision_citations (source_decision_id);
CREATE INDEX IF NOT EXISTS idx_deccit_date   ON decision_citations (citing_date);


CREATE TABLE IF NOT EXISTS decision_statutes (
    source_decision_id  TEXT NOT NULL,
    sr_number           TEXT NOT NULL,
    article_num         TEXT,
    paragraph           TEXT,
    raw_text            TEXT,
    n_mentions          INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (source_decision_id, sr_number, article_num, paragraph)
);

CREATE INDEX IF NOT EXISTS idx_decstat_sr_article ON decision_statutes (sr_number, article_num);
CREATE INDEX IF NOT EXISTS idx_decstat_source     ON decision_statutes (source_decision_id);


-- Unresolved citation targets (dangling references — useful for diagnostics).
CREATE TABLE IF NOT EXISTS citation_targets (
    raw_docket          TEXT PRIMARY KEY,
    normalized_docket   TEXT,
    n_occurrences       INTEGER NOT NULL DEFAULT 1,
    first_seen          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_citt_normalised ON citation_targets (normalized_docket);
