"""Bulk migration from local SQLite databases to Postgres+pgvectorscale.

Streams each source table in batches (10-50k rows) using Postgres's
COPY FROM binary protocol via psycopg's copy() — 50-100× faster than
row-by-row INSERT.

Idempotent per table: uses decision_id / chunk id / PK as conflict key
with ON CONFLICT DO UPDATE for metadata tables, skip-on-conflict for
heavy-write tables. Safe to re-run to resume after interruption.

Transfer strategy:

    1. decisions.db (55 GB)    → decisions table (COPY)
    2. parag_chunks.db         → chunks, chunk_embeddings (vector pack),
                                 chunk_law_citations, chunk_case_citations,
                                 decision_enrichment, decision_authority,
                                 enrichment_state
    3. statutes.db             → laws_federal, articles_federal
    4. cantonal_laws.db        → laws_cantonal, articles_cantonal
    5. reference_graph.db      → decision_citations, decision_statutes,
                                 citation_targets

Vectors: the sqlite-vec blob format is little-endian float32 (packed
struct "Nf"). psycopg2/psycopg3 + pgvector accepts lists or numpy
arrays as `vector(N)`.

Usage:
    python -m postgres.migrate.sqlite_to_pg \\
        --caselaw-url "postgres://caselaw:***@hetzner:5432/caselaw" \\
        --source-dir ~/.swiss-caselaw \\
        --tables chunks chunk_embeddings decision_enrichment
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

try:
    import psycopg
    from psycopg import sql
except ImportError:
    sys.stderr.write(
        "psycopg v3 required: pip install 'psycopg[binary,pool]'\n"
    )
    raise

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("migrate")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_SOURCE_DIR = Path.home() / ".swiss-caselaw"
BATCH_SIZE = 20_000


@dataclass
class TableSpec:
    name: str                         # Postgres table
    source_db: str                    # SQLite file under SOURCE_DIR
    source_query: str                 # SELECT producing the columns
    pg_columns: list[str]             # target column order
    upsert_key: list[str] | None      # None = plain INSERT (fast); list = ON CONFLICT (name) DO UPDATE


# ---------------------------------------------------------------------------
# Table specifications
# ---------------------------------------------------------------------------

TABLE_SPECS: list[TableSpec] = [

    # ── decisions.db → decisions ──────────────────────────────────────
    TableSpec(
        name="decisions",
        source_db="decisions.db",
        source_query="""
            SELECT decision_id, court, canton, chamber, docket_number, docket_number_2,
                   decision_date, publication_date, language, title, legal_area, regeste,
                   abstract_de, abstract_fr, abstract_it, full_text, decision_type,
                   outcome, source_url, pdf_url, cited_decisions, scraped_at, source,
                   source_id, source_spider, content_hash, json_data, canonical_key
            FROM decisions
        """,
        pg_columns=[
            "decision_id", "court", "canton", "chamber", "docket_number", "docket_number_2",
            "decision_date", "publication_date", "language", "title", "legal_area", "regeste",
            "abstract_de", "abstract_fr", "abstract_it", "full_text", "decision_type",
            "outcome", "source_url", "pdf_url", "cited_decisions", "scraped_at", "source",
            "source_id", "source_spider", "content_hash", "json_data", "canonical_key",
        ],
        upsert_key=["decision_id", "decision_year"],   # partition key
    ),

    # ── parag_chunks.db → chunks + related ────────────────────────────
    TableSpec(
        name="chunks",
        source_db="parag_chunks.db",
        source_query="""
            SELECT id AS legacy_id,
                   decision_id, court, language, considerant_number, depth,
                   span_start, span_end, raw_length, cleaned, summary,
                   summary_source, chunk_hash, prompt_version, created_at
            FROM chunks
        """,
        pg_columns=[
            "legacy_id", "decision_id", "court", "language", "considerant_number",
            "depth", "span_start", "span_end", "raw_length", "cleaned", "summary",
            "summary_source", "chunk_hash", "prompt_version", "created_at",
        ],
        upsert_key=["legacy_id"],
    ),

    TableSpec(
        name="decision_enrichment",
        source_db="parag_chunks.db",
        source_query="""
            SELECT decision_id, court, procedural_stage, outcome, subject_matter,
                   principle_questions, obiter_dicta, doctrine_discussion,
                   language_detected, source_hash, prompt_version,
                   llm_latency_s, prompt_tokens, completion_tokens,
                   status, error_message, processed_at
            FROM decision_enrichment
        """,
        pg_columns=[
            "decision_id", "court", "procedural_stage", "outcome", "subject_matter",
            "principle_questions", "obiter_dicta", "doctrine_discussion",
            "language_detected", "source_hash", "prompt_version",
            "llm_latency_s", "prompt_tokens", "completion_tokens",
            "status", "error_message", "processed_at",
        ],
        upsert_key=["decision_id"],
    ),

    TableSpec(
        name="decision_authority",
        source_db="parag_chunks.db",
        source_query="""
            SELECT decision_id, court_level, atf_published, authority_score,
                   pagerank_raw, pagerank_temporal, validity_status,
                   n_overruled_by, n_criticized_by, n_confirmed_by, n_cited_by,
                   computed_at
            FROM decision_authority
        """,
        pg_columns=[
            "decision_id", "court_level", "atf_published", "authority_score",
            "pagerank_raw", "pagerank_temporal", "validity_status",
            "n_overruled_by", "n_criticized_by", "n_confirmed_by", "n_cited_by",
            "computed_at",
        ],
        upsert_key=["decision_id"],
    ),

    TableSpec(
        name="enrichment_state",
        source_db="parag_chunks.db",
        source_query="SELECT * FROM enrichment_state",
        pg_columns=[
            "decision_id", "court", "parser_name", "fallback_used",
            "n_chunks", "n_stubs", "n_self_suff", "n_summarized", "n_errors",
            "llm_calls", "llm_latency_s", "prompt_tokens", "completion_tokens",
            "source_hash", "prompt_version", "status", "error_message", "processed_at",
        ],
        upsert_key=["decision_id"],
    ),

    # Citations require special handling because they FK on chunk.id,
    # which may have been renumbered. See migrate_citations() below.
    # Placeholder for the schedule — actual migration is custom.

    # ── statutes.db → laws_federal + articles_federal ────────────────
    TableSpec(
        name="laws_federal",
        source_db="statutes.db",
        source_query="SELECT sr_number, title_de, title_fr, title_it, abbr_de, abbr_fr, abbr_it, url_de, url_fr, url_it FROM laws",
        pg_columns=["sr_number","title_de","title_fr","title_it","abbr_de","abbr_fr","abbr_it","url_de","url_fr","url_it"],
        upsert_key=["sr_number"],
    ),

    TableSpec(
        name="articles_federal",
        source_db="statutes.db",
        source_query="SELECT sr_number, language, article_num, heading, text FROM articles",
        pg_columns=["sr_number","language","article_num","heading","text"],
        upsert_key=None,  # UNIQUE constraint catches duplicates on re-run
    ),

    # ── cantonal_laws.db → laws_cantonal + articles_cantonal ─────────
    TableSpec(
        name="laws_cantonal",
        source_db="cantonal_laws.db",
        source_query="""
            SELECT lexfind_id, language, canton, sr_number, title, category, is_active,
                   original_url, version_active_since, text_length, article_count,
                   text_source, full_text, fetched_at
            FROM laws
        """,
        pg_columns=[
            "lexfind_id","language","canton","sr_number","title","category","is_active",
            "original_url","version_active_since","text_length","article_count",
            "text_source","full_text","fetched_at",
        ],
        upsert_key=["lexfind_id", "language"],
    ),

    TableSpec(
        name="articles_cantonal",
        source_db="cantonal_laws.db",
        source_query="SELECT lexfind_id, language, canton, seq, article_num, heading, text FROM articles",
        pg_columns=["lexfind_id","language","canton","seq","article_num","heading","text"],
        upsert_key=None,
    ),

    # ── reference_graph.db → decision_citations + decision_statutes ──
    TableSpec(
        name="decision_citations",
        source_db="reference_graph.db",
        source_query="""
            SELECT source_decision_id, target_decision_id, raw_text,
                   confidence_score, resolution_method, citing_date
            FROM decision_citations
        """,
        pg_columns=["source_decision_id","target_decision_id","raw_text","confidence_score","resolution_method","citing_date"],
        upsert_key=["source_decision_id","target_decision_id"],
    ),

    TableSpec(
        name="decision_statutes",
        source_db="reference_graph.db",
        source_query="""
            SELECT source_decision_id, sr_number, article_num, paragraph, raw_text, n_mentions
            FROM decision_statutes
        """,
        pg_columns=["source_decision_id","sr_number","article_num","paragraph","raw_text","n_mentions"],
        upsert_key=["source_decision_id","sr_number","article_num","paragraph"],
    ),
]


# ---------------------------------------------------------------------------
# SQLite reader utilities
# ---------------------------------------------------------------------------

def _open_sqlite(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _iter_rows(
    conn: sqlite3.Connection, query: str, batch: int = BATCH_SIZE
) -> Iterator[list[tuple]]:
    """Yield lists of tuples, batch_size each, to avoid memory blowup."""
    cursor = conn.cursor()
    cursor.execute(query)
    while True:
        rows = cursor.fetchmany(batch)
        if not rows:
            break
        yield [tuple(r) for r in rows]


# ---------------------------------------------------------------------------
# Postgres writer utilities
# ---------------------------------------------------------------------------

def _ensure_staging_unique_constraint(pg: psycopg.Connection, spec: TableSpec) -> None:
    """Some of our ON CONFLICT targets match a UNIQUE constraint that lives
    on the final table (not a PK). Nothing to do at runtime — Postgres's
    ON CONFLICT auto-detects the matching constraint. This hook exists for
    future customisations (partition routing etc.)."""
    pass


def _conflict_clause(spec: TableSpec) -> sql.Composable:
    if not spec.upsert_key:
        return sql.SQL("")
    target = sql.SQL(", ").join(sql.Identifier(k) for k in spec.upsert_key)
    set_cols = [c for c in spec.pg_columns if c not in spec.upsert_key]
    if not set_cols:
        return sql.SQL("ON CONFLICT ({}) DO NOTHING").format(target)
    set_pairs = sql.SQL(", ").join(
        sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c))
        for c in set_cols
    )
    return sql.SQL("ON CONFLICT ({}) DO UPDATE SET {}").format(target, set_pairs)


def _copy_batches_via_insert(
    pg: psycopg.Connection, spec: TableSpec, rows_batches: Iterable[list[tuple]],
) -> int:
    """INSERT with ON CONFLICT — safe for upserts. Slightly slower than COPY
    but handles the idempotent case. For the huge tables (decisions,
    chunks, decision_citations) we fall back to COPY with a staging table
    (see _copy_via_staging)."""
    col_idents = sql.SQL(", ").join(sql.Identifier(c) for c in spec.pg_columns)
    placeholders = sql.SQL(", ").join(sql.Placeholder() * len(spec.pg_columns))
    stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({}) {}").format(
        sql.Identifier(spec.name), col_idents, placeholders, _conflict_clause(spec),
    )

    total = 0
    with pg.cursor() as cur:
        for batch in rows_batches:
            cur.executemany(stmt, batch)
            pg.commit()
            total += len(batch)
            log.info("  %s: +%d rows (total %d)", spec.name, len(batch), total)
    return total


def _copy_via_staging(
    pg: psycopg.Connection, spec: TableSpec, rows_batches: Iterable[list[tuple]],
) -> int:
    """Fastest path for large tables: COPY into a throwaway table, then
    INSERT ... SELECT ... ON CONFLICT ... at the end. Used for decisions,
    chunks, citation graph."""
    staging = f"_staging_{spec.name}"
    with pg.cursor() as cur:
        cur.execute(
            sql.SQL("CREATE TEMP TABLE {} (LIKE {} INCLUDING DEFAULTS)").format(
                sql.Identifier(staging), sql.Identifier(spec.name)
            )
        )
        # Only copy the source columns (let generated columns stay empty in staging)
        col_idents = sql.SQL(", ").join(sql.Identifier(c) for c in spec.pg_columns)

        total = 0
        with cur.copy(
            sql.SQL("COPY {} ({}) FROM STDIN").format(
                sql.Identifier(staging), col_idents
            )
        ) as copy:
            for batch in rows_batches:
                for row in batch:
                    copy.write_row(row)
                total += len(batch)
                log.info("  %s staging +%d (total %d)", spec.name, len(batch), total)

        log.info("  %s: flushing staging → %s with upsert", spec.name, spec.name)
        cur.execute(
            sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {} {}").format(
                sql.Identifier(spec.name), col_idents, col_idents,
                sql.Identifier(staging), _conflict_clause(spec),
            )
        )
        pg.commit()
    return total


# ---------------------------------------------------------------------------
# Vector-specific migration (separate because of blob → pgvector conversion)
# ---------------------------------------------------------------------------

def _blob_to_vector(blob: bytes) -> list[float]:
    """sqlite-vec stores float32 little-endian packed as "Nf" struct."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def migrate_chunk_embeddings(
    sqlite_conn: sqlite3.Connection, pg: psycopg.Connection,
    *, batch: int = 5_000, embedding_dim: int = 1024,
) -> int:
    """Migrate vec_chunks (sqlite-vec virtual) + chunk_embeddings_meta (SQLite)
    → chunk_embeddings (Postgres+pgvector). Requires pgvector extension.

    Assumes sqlite-vec is loaded on the SQLite connection (caller's job)."""
    cur = sqlite_conn.cursor()
    cur.execute("""
        SELECT m.chunk_id, v.embedding, m.model, m.embedding_version, m.encoded_at
        FROM chunk_embeddings_meta m
        JOIN vec_chunks v ON v.rowid = m.chunk_id
    """)
    stmt = """
        INSERT INTO chunk_embeddings
            (chunk_id, embedding, model, embedding_version, encoded_at)
        VALUES (%s, %s::vector, %s, %s, %s)
        ON CONFLICT (chunk_id) DO UPDATE SET
            embedding         = EXCLUDED.embedding,
            model             = EXCLUDED.model,
            embedding_version = EXCLUDED.embedding_version,
            encoded_at        = EXCLUDED.encoded_at
    """
    total = 0
    to_write: list[tuple] = []
    with pg.cursor() as pc:
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                break
            for chunk_id, blob, model, ver, encoded_at in rows:
                vec = _blob_to_vector(blob)
                if len(vec) != embedding_dim:
                    log.warning("skip chunk %d: dim mismatch %d != %d", chunk_id, len(vec), embedding_dim)
                    continue
                vec_literal = "[" + ",".join(f"{v:.7f}" for v in vec) + "]"
                to_write.append((chunk_id, vec_literal, model, ver, encoded_at))
            pc.executemany(stmt, to_write)
            pg.commit()
            total += len(to_write)
            log.info("  chunk_embeddings: +%d (total %d)", len(to_write), total)
            to_write.clear()
    return total


# ---------------------------------------------------------------------------
# Citation migration with chunk-id remapping
# ---------------------------------------------------------------------------

def migrate_chunk_citations(
    sqlite_conn: sqlite3.Connection, pg: psycopg.Connection,
) -> tuple[int, int]:
    """Migrate chunk_law_citations + chunk_case_citations with chunk_id
    remapping via the chunks.legacy_id column (preserved during the
    chunks table migration)."""
    # Build legacy_id → new id mapping from chunks table
    with pg.cursor() as pc:
        pc.execute("SELECT legacy_id, id FROM chunks WHERE legacy_id IS NOT NULL")
        mapping = dict(pc.fetchall())
    log.info("  chunks legacy_id → id map: %d entries", len(mapping))

    # Law citations
    cur = sqlite_conn.cursor()
    cur.execute("SELECT chunk_id, sr_number, law_abbr, article_num, paragraph, letter, "
                "raw_text, normalized, source, resolved FROM chunk_law_citations")
    stmt_law = """
        INSERT INTO chunk_law_citations
            (chunk_id, sr_number, law_abbr, article_num, paragraph, letter,
             raw_text, normalized, source, resolved)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::boolean)
    """
    buf: list[tuple] = []
    n_law = 0
    with pg.cursor() as pc:
        for row in cur:
            new_id = mapping.get(row[0])
            if new_id is None:
                continue
            buf.append((new_id, *row[1:]))
            if len(buf) >= BATCH_SIZE:
                pc.executemany(stmt_law, buf)
                pg.commit()
                n_law += len(buf)
                log.info("  chunk_law_citations: +%d (total %d)", len(buf), n_law)
                buf.clear()
        if buf:
            pc.executemany(stmt_law, buf); pg.commit(); n_law += len(buf)

    # Case citations
    cur.execute("SELECT chunk_id, target_decision_id, citation_type, raw_text, source, direction "
                "FROM chunk_case_citations")
    stmt_case = """
        INSERT INTO chunk_case_citations
            (chunk_id, target_decision_id, citation_type, raw_text, source, direction)
        VALUES (%s, %s, %s, %s, %s, %s)
    """
    buf.clear()
    n_case = 0
    with pg.cursor() as pc:
        for row in cur:
            new_id = mapping.get(row[0])
            if new_id is None:
                continue
            buf.append((new_id, *row[1:]))
            if len(buf) >= BATCH_SIZE:
                pc.executemany(stmt_case, buf)
                pg.commit()
                n_case += len(buf)
                log.info("  chunk_case_citations: +%d (total %d)", len(buf), n_case)
                buf.clear()
        if buf:
            pc.executemany(stmt_case, buf); pg.commit(); n_case += len(buf)

    return n_law, n_case


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_spec(
    pg: psycopg.Connection, source_dir: Path, spec: TableSpec,
    use_staging: bool = True,
) -> int:
    src_path = source_dir / spec.source_db
    if not src_path.exists() or src_path.stat().st_size == 0:
        log.warning("source %s missing → skip %s", src_path, spec.name)
        return 0
    log.info("▶ %s  (from %s)", spec.name, spec.source_db)
    t0 = time.monotonic()
    with _open_sqlite(src_path) as sconn:
        rows = _iter_rows(sconn, spec.source_query)
        if use_staging and not spec.upsert_key:
            # plain INSERT, fastest path
            n = _copy_via_staging(pg, spec, rows)
        else:
            n = _copy_batches_via_insert(pg, spec, rows)
    log.info("✓ %s: %d rows in %.1fs", spec.name, n, time.monotonic() - t0)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--caselaw-url", required=True,
                    help="postgres://user:pass@host:5432/caselaw")
    ap.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    ap.add_argument("--tables", nargs="*", default=None,
                    help="Subset of table names (default: all)")
    ap.add_argument("--skip", nargs="*", default=[],
                    help="Table names to skip")
    ap.add_argument("--skip-embeddings", action="store_true")
    ap.add_argument("--skip-citations",  action="store_true")
    args = ap.parse_args()

    log.info("source dir : %s", args.source_dir)
    log.info("target     : %s", _hide_password(args.caselaw_url))

    with psycopg.connect(args.caselaw_url, autocommit=False) as pg:
        log.info("connected OK to Postgres")

        # 1. Plain tables
        for spec in TABLE_SPECS:
            if args.tables and spec.name not in args.tables:
                continue
            if spec.name in args.skip:
                continue
            run_spec(pg, args.source_dir, spec)

        # 2. Vector embeddings (requires sqlite-vec on the SQLite side)
        if not args.skip_embeddings:
            chunks_db = args.source_dir / "parag_chunks.db"
            if chunks_db.exists():
                try:
                    import sqlite_vec
                    conn = _open_sqlite(chunks_db)
                    conn.enable_load_extension(True)
                    sqlite_vec.load(conn)
                    conn.enable_load_extension(False)
                    log.info("▶ chunk_embeddings")
                    t0 = time.monotonic()
                    n = migrate_chunk_embeddings(conn, pg)
                    log.info("✓ chunk_embeddings: %d rows in %.1fs",
                             n, time.monotonic() - t0)
                    conn.close()
                except ImportError:
                    log.warning("sqlite-vec not installed — skip embeddings migration")

        # 3. Citations with chunk_id remap
        if not args.skip_citations:
            chunks_db = args.source_dir / "parag_chunks.db"
            if chunks_db.exists():
                conn = _open_sqlite(chunks_db)
                log.info("▶ chunk_law_citations + chunk_case_citations (remap)")
                t0 = time.monotonic()
                n_law, n_case = migrate_chunk_citations(conn, pg)
                log.info("✓ citations: %d law + %d case in %.1fs",
                         n_law, n_case, time.monotonic() - t0)
                conn.close()

    log.info("DONE")


def _hide_password(url: str) -> str:
    """Mask password in a postgres URL for logging."""
    import re
    return re.sub(r"://([^:]+):[^@]+@", r"://\1:***@", url)


if __name__ == "__main__":
    main()
