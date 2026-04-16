"""PA-RAG SQLite schema (Phase 3 SAC + future Phase 5 enrichment).

Kept strictly separate from db_schema.py (upstream, unstable) per the
isolation rule. Creates its own file at ~/.swiss-caselaw/parag_chunks.db
so merging upstream changes never touches our schema.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_PARAG_DB = Path.home() / ".swiss-caselaw" / "parag_chunks.db"

#: Bump when a change in the SAC prompt / builder logic requires
#: reprocessing previously-stored chunks. The worker compares against the
#: `prompt_version` recorded per-decision and re-runs if current > stored.
PROMPT_VERSION = 1

#: Embedding model + version. Bump EMBEDDING_VERSION if we change the model
#: or its preprocessing (e.g. summary-prepend strategy). Existing vectors
#: with a smaller version will be re-encoded by the embed worker.
EMBEDDING_MODEL = "BAAI/bge-m3"
EMBEDDING_DIM = 1024
EMBEDDING_VERSION = 1

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS chunks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id         TEXT NOT NULL,
    court               TEXT NOT NULL,
    language            TEXT NOT NULL,
    considerant_number  TEXT,        -- "3", "3.1", "3.1.2", or "implicit"
    depth               INTEGER,     -- 1 for "3", 2 for "3.1", 0 for implicit
    span_start          INTEGER NOT NULL,
    span_end            INTEGER NOT NULL,
    raw_length          INTEGER,
    cleaned             TEXT NOT NULL,
    summary             TEXT,        -- LLM-generated header, or NULL if skipped
    summary_source      TEXT,        -- 'stub' | 'self_sufficient' | 'llm' | 'error'
    chunk_hash          TEXT,        -- hash of cleaned content for dedup/checks
    prompt_version      INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(decision_id, considerant_number, span_start)
);

CREATE INDEX IF NOT EXISTS idx_chunks_decision       ON chunks(decision_id);
CREATE INDEX IF NOT EXISTS idx_chunks_court          ON chunks(court);
CREATE INDEX IF NOT EXISTS idx_chunks_summary_source ON chunks(summary_source);
CREATE INDEX IF NOT EXISTS idx_chunks_prompt_version ON chunks(prompt_version);

-- Per-decision processing state for incremental / resumable runs.
CREATE TABLE IF NOT EXISTS enrichment_state (
    decision_id     TEXT PRIMARY KEY,
    court           TEXT NOT NULL,
    parser_name     TEXT,
    fallback_used   INTEGER DEFAULT 0,   -- number of LLM fallback events
    n_chunks        INTEGER,
    n_stubs         INTEGER,
    n_self_suff     INTEGER,
    n_summarized    INTEGER,
    n_errors        INTEGER,
    llm_calls       INTEGER,
    llm_latency_s   REAL,
    prompt_tokens   INTEGER,
    completion_tokens INTEGER,
    source_hash     TEXT,                -- hash(decisions.full_text) for skip-if-unchanged
    prompt_version  INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL,       -- 'ok' | 'error' | 'empty'
    error_message   TEXT,
    processed_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_state_court   ON enrichment_state(court);
CREATE INDEX IF NOT EXISTS idx_state_status  ON enrichment_state(status);

-- Phase 4 embedding bookkeeping.
-- The actual vectors live in a sqlite-vec virtual table (vec_chunks),
-- created separately after sqlite_vec.load() is called on the connection.
-- This regular table tracks WHICH chunks have been encoded with WHICH
-- model version, so we can incrementally re-encode on model upgrades.
CREATE TABLE IF NOT EXISTS chunk_embeddings_meta (
    chunk_id           INTEGER PRIMARY KEY,
    model              TEXT NOT NULL,
    embedding_version  INTEGER NOT NULL,
    encoded_at         TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cemb_version ON chunk_embeddings_meta(embedding_version);

-- Phase 5: resolved law-article citations extracted per chunk.
-- Fed by citation_resolver.resolve_and_store(), sourced from either
-- the LLM enrichment output ('legal_basis', etc.) or raw-text regex
-- extraction on the chunk body.
CREATE TABLE IF NOT EXISTS chunk_law_citations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id            INTEGER NOT NULL,
    sr_number           TEXT,                 -- NULL if not resolved against statutes.db
    law_abbr            TEXT NOT NULL,        -- "LTF", "CC", "ZGB", ...
    article_num         TEXT,                 -- "93", "93bis", "93a", NULL if not parsed
    paragraph           TEXT,                 -- "1", "3bis", NULL if none
    letter              TEXT,                 -- "a", "b", NULL if none
    raw_text            TEXT NOT NULL,        -- original string "art. 93 al. 1 let. a LTF"
    normalized          TEXT NOT NULL,        -- canonical form
    source              TEXT NOT NULL,        -- 'llm' | 'regex' | 'both'
    resolved            INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_clc_chunk   ON chunk_law_citations(chunk_id);
CREATE INDEX IF NOT EXISTS idx_clc_law     ON chunk_law_citations(law_abbr, article_num);
CREATE INDEX IF NOT EXISTS idx_clc_sr      ON chunk_law_citations(sr_number, article_num);
CREATE INDEX IF NOT EXISTS idx_clc_norm    ON chunk_law_citations(normalized);

-- Case citations extracted per chunk (feed authority/temporal graph).
CREATE TABLE IF NOT EXISTS chunk_case_citations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id            INTEGER NOT NULL,
    target_decision_id  TEXT NOT NULL,        -- canonical: "BGE 128 IV 225" or "BGer 6B_123_2019"
    citation_type       TEXT NOT NULL,        -- 'bge' | 'docket'
    raw_text            TEXT NOT NULL,
    source              TEXT NOT NULL,        -- 'llm' | 'regex' | 'both'
    direction           TEXT,                 -- from LLM prior_case_treatment: confirms|overrules|...
    FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ccc_chunk   ON chunk_case_citations(chunk_id);
CREATE INDEX IF NOT EXISTS idx_ccc_target  ON chunk_case_citations(target_decision_id);

-- Per-decision enrichment (Phase 5) results. Kept separate from
-- enrichment_state (which tracks SAC-level processing).
CREATE TABLE IF NOT EXISTS decision_enrichment (
    decision_id         TEXT PRIMARY KEY,
    court               TEXT NOT NULL,
    procedural_stage    TEXT,                  -- 'recours' | 'premiere_instance'
    outcome             TEXT,                  -- 'admission' | 'admission_partielle' | 'rejet' | 'irrecevabilite'
    subject_matter      TEXT,
    principle_questions TEXT,                  -- JSON array of {question, ratio, legal_basis}
    obiter_dicta        TEXT,                  -- JSON array of strings
    doctrine_discussion TEXT,                  -- JSON object
    language_detected   TEXT,                  -- de | fr | it — body-text language
    source_hash         TEXT,
    prompt_version      INTEGER NOT NULL DEFAULT 1,
    llm_latency_s       REAL,
    prompt_tokens       INTEGER,
    completion_tokens   INTEGER,
    status              TEXT NOT NULL,         -- 'ok' | 'error' | 'parse_fail'
    error_message       TEXT,
    processed_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_denrich_outcome ON decision_enrichment(outcome);
CREATE INDEX IF NOT EXISTS idx_denrich_status  ON decision_enrichment(status);

-- Per-decision authority scoring (pilier 1 + 2 + 3 of PA-RAG).
-- Populated by post-processing jobs AFTER enrichment runs complete.
CREATE TABLE IF NOT EXISTS decision_authority (
    decision_id       TEXT PRIMARY KEY,
    court_level       INTEGER,            -- 1 (admin) → 5 (TF)
    atf_published     INTEGER DEFAULT 0,  -- 1 if in Recueil Officiel
    authority_score   REAL,               -- composite static score
    pagerank_raw      REAL,               -- unweighted PageRank
    pagerank_temporal REAL,               -- time-decayed PageRank
    validity_status   TEXT,               -- 'valid'|'overruled'|'criticized'|'distinguished'
    n_overruled_by    INTEGER DEFAULT 0,  -- count of decisions that overrule this one
    n_criticized_by   INTEGER DEFAULT 0,
    n_confirmed_by    INTEGER DEFAULT 0,
    n_cited_by        INTEGER DEFAULT 0,  -- total inbound citations
    computed_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_dauth_level  ON decision_authority(court_level);
CREATE INDEX IF NOT EXISTS idx_dauth_valid  ON decision_authority(validity_status);
CREATE INDEX IF NOT EXISTS idx_dauth_score  ON decision_authority(authority_score DESC);
CREATE INDEX IF NOT EXISTS idx_dauth_prank  ON decision_authority(pagerank_temporal DESC);
"""


def init_parag_schema(path: str | Path = DEFAULT_PARAG_DB) -> sqlite3.Connection:
    """Create the chunks DB file and tables if they don't exist; return an
    open connection with WAL mode enabled.

    `check_same_thread=False` lets the worker pool share a single connection
    — we synchronise writes via an explicit threading.Lock in the worker.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30.0, check_same_thread=False)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def connect(path: str | Path = DEFAULT_PARAG_DB) -> sqlite3.Connection:
    """Open an existing chunks DB. Raises if it does not exist."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"Parag chunks DB not found at {p}. Run init_parag_schema() first."
        )
    conn = sqlite3.connect(str(p), timeout=30.0, check_same_thread=False)
    return conn
