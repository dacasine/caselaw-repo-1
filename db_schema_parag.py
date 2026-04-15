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
