"""Weekly law update — scrape Fedlex + LexFind, diff against article_versions, upsert changes.

Flow:
    1. Rebuild statutes.db from Fedlex (reuse build_statutes_db.py)
    2. Rebuild cantonal_laws.db from LexFind (reuse build_cantonal_laws_db.py)
    3. Diff against article_versions (in_force) in Postgres
    4. Upsert changes: new articles, modified articles, abrogated articles
    5. Update articles_federal / articles_cantonal with current version
    6. Log all detected changes

Usage:
    PYTHONPATH=/srv/caselaw .venv/bin/python scripts/laws/update_laws.py [--skip-scrape] [--federal-only] [--cantonal-only]
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import logging
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from search_stack.parag.pg_conn import get_pg_url, _load_env

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("update-laws")

STATUTES_DB = Path("/srv/data/statutes.db")
CANTONAL_DB = Path("/srv/data/cantonal_laws.db")
TODAY = date.today()


def _fix_date(d: str | None) -> str:
    """Convert DD.MM.YYYY to YYYY-MM-DD, or return as-is if already ISO."""
    if not d or not d.strip():
        return str(TODAY)
    d = d.strip()
    if "." in d and len(d) == 10:
        parts = d.split(".")
        if len(parts) == 3 and len(parts[2]) == 4:
            return f"{parts[2]}-{parts[1]}-{parts[0]}"
    if d.startswith("0000"):
        return str(TODAY)
    return d


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _make_diff_summary(old_text: str, new_text: str) -> str:
    """Generate a human-readable diff summary."""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff = list(difflib.unified_diff(old_lines, new_lines, n=1))
    if not diff:
        return ""
    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    # Extract first meaningful change
    changes = [l.strip() for l in diff if (l.startswith("+") or l.startswith("-"))
               and not l.startswith("+++") and not l.startswith("---")]
    excerpt = changes[0][:150] if changes else ""
    return f"+{added}/-{removed} lignes. {excerpt}"


def sync_federal(pg: psycopg.Connection, skip_scrape: bool = False):
    """Sync federal articles from statutes.db → article_versions + articles_federal."""
    if not skip_scrape:
        log.info("Scraping Fedlex...")
        import subprocess
        result = subprocess.run(
            [sys.executable, "search_stack/build_statutes_db.py"],
            capture_output=True, text=True, cwd="/srv/caselaw"
        )
        if result.returncode != 0:
            log.error("Fedlex scrape failed: %s", result.stderr[-500:])
            return {"status": "scrape_failed"}
        log.info("Fedlex scrape complete")

    if not STATUTES_DB.exists():
        log.warning("statutes.db not found")
        return {"status": "no_db"}

    sq = sqlite3.connect(f"file:{STATUTES_DB}?mode=ro", uri=True)
    sq.row_factory = sqlite3.Row

    # Load all current articles from SQLite
    fresh = {}
    for r in sq.execute("SELECT sr_number, lang, article_num, heading, footnote, text FROM articles"):
        key = (r["sr_number"], r["lang"], r["article_num"])
        fresh[key] = {
            "heading": r["heading"], "footnote": r["footnote"], "text": r["text"]
        }
    log.info("Fedlex: %d fresh articles from statutes.db", len(fresh))

    # Load consolidation dates for valid_from
    law_dates = {}
    for r in sq.execute("SELECT sr_number, consolidation_date FROM laws"):
        law_dates[r["sr_number"]] = r["consolidation_date"] or str(TODAY)
    sq.close()

    # Load current in_force from Postgres
    current = {}
    rows = pg.execute(
        "SELECT id, sr_number, lang, article_num, text, heading, footnote, valid_from "
        "FROM article_versions WHERE jurisdiction = 'federal' AND status = 'in_force'"
    ).fetchall()
    for r in rows:
        key = (r[1], r[2], r[3])
        current[key] = {"id": r[0], "text": r[4], "heading": r[5], "footnote": r[6], "valid_from": r[7]}
    log.info("Postgres: %d in_force federal articles", len(current))

    stats = {"new": 0, "modified": 0, "abrogated": 0, "unchanged": 0}

    with pg.cursor() as cur:
        # Process fresh articles
        for key, art in fresh.items():
            sr, lang, anum = key
            valid_from_str = law_dates.get(sr, str(TODAY))

            if key not in current:
                # New article
                cur.execute("""
                    INSERT INTO article_versions
                        (jurisdiction, sr_number, lang, article_num, heading, text, footnote,
                         valid_from, status, change_type, source)
                    VALUES ('federal', %s, %s, %s, %s, %s, %s, %s, 'in_force', 'initial', 'fedlex')
                    ON CONFLICT DO NOTHING
                """, (sr, lang, anum, art["heading"], art["text"], art["footnote"], valid_from_str))
                stats["new"] += 1
            elif art["text"] != current[key]["text"] or (art["heading"] or "") != (current[key]["heading"] or ""):
                # Modified — supersede old, insert new
                old = current[key]
                diff_summary = _make_diff_summary(old["text"] or "", art["text"])
                # Supersede old version
                cur.execute(
                    "UPDATE article_versions SET valid_to = %s, status = 'superseded' WHERE id = %s",
                    (TODAY, old["id"])
                )
                # Insert new version
                cur.execute("""
                    INSERT INTO article_versions
                        (jurisdiction, sr_number, lang, article_num, heading, text, footnote,
                         valid_from, status, change_type, predecessor_id, diff_summary, source)
                    VALUES ('federal', %s, %s, %s, %s, %s, %s, %s, 'in_force', 'modified', %s, %s, 'fedlex')
                    ON CONFLICT DO NOTHING
                """, (sr, lang, anum, art["heading"], art["text"], art["footnote"],
                      TODAY, old["id"], diff_summary))
                stats["modified"] += 1
                log.info("  MODIFIED: %s art. %s (%s) — %s", sr, anum, lang, diff_summary[:80])
            else:
                stats["unchanged"] += 1

        # Check for abrogated articles (in DB but not in fresh scrape)
        for key, old in current.items():
            if key not in fresh:
                cur.execute(
                    "UPDATE article_versions SET valid_to = %s, status = 'abrogated', change_type = 'abrogated' WHERE id = %s",
                    (TODAY, old["id"])
                )
                stats["abrogated"] += 1
                sr, lang, anum = key
                log.info("  ABROGATED: %s art. %s (%s)", sr, anum, lang)

        # Also update articles_federal (current version table)
        cur.execute("DELETE FROM articles_federal")
        for key, art in fresh.items():
            sr, lang, anum = key
            cur.execute(
                "INSERT INTO articles_federal (sr_number, lang, article_num, heading, footnote, text) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (sr, lang, anum, art["heading"], art["footnote"], art["text"])
            )

    pg.commit()
    log.info("Federal: %s", stats)
    return stats


def sync_cantonal(pg: psycopg.Connection, skip_scrape: bool = False):
    """Sync cantonal articles from cantonal_laws.db → article_versions + articles_cantonal."""
    if not skip_scrape:
        log.info("Scraping LexFind + cantonal portals...")
        import subprocess
        result = subprocess.run(
            [sys.executable, "search_stack/build_cantonal_laws_db.py"],
            capture_output=True, text=True, cwd="/srv/caselaw"
        )
        if result.returncode != 0:
            log.error("Cantonal scrape failed: %s", result.stderr[-500:])
            return {"status": "scrape_failed"}
        log.info("Cantonal scrape complete")

    if not CANTONAL_DB.exists():
        log.warning("cantonal_laws.db not found")
        return {"status": "no_db"}

    sq = sqlite3.connect(f"file:{CANTONAL_DB}?mode=ro", uri=True)
    sq.row_factory = sqlite3.Row

    # Load fresh articles
    fresh = {}
    for r in sq.execute("""
        SELECT a.lexfind_id, a.language, a.canton, a.article_num, a.heading, a.text,
               l.sr_number, l.version_active_since
        FROM articles a JOIN laws l ON l.lexfind_id = a.lexfind_id AND l.language = a.language
    """):
        sr = r["sr_number"] or str(r["lexfind_id"])
        key = (sr, r["canton"], r["language"], r["article_num"] or str(r.keys()))
        fresh[key] = {
            "heading": r["heading"], "text": r["text"],
            "valid_from": _fix_date(r["version_active_since"]),
            "canton": r["canton"],
        }
    log.info("Cantonal: %d fresh articles from cantonal_laws.db", len(fresh))
    sq.close()

    # Load current in_force
    current = {}
    rows = pg.execute(
        "SELECT id, sr_number, canton, lang, article_num, text, heading, valid_from "
        "FROM article_versions WHERE jurisdiction = 'cantonal' AND status = 'in_force'"
    ).fetchall()
    for r in rows:
        key = (r[1], r[2], r[3], r[4])
        current[key] = {"id": r[0], "text": r[5], "heading": r[6], "valid_from": r[7]}
    log.info("Postgres: %d in_force cantonal articles", len(current))

    stats = {"new": 0, "modified": 0, "abrogated": 0, "unchanged": 0}

    with pg.cursor() as cur:
        for key, art in fresh.items():
            sr, canton, lang, anum = key
            if key not in current:
                cur.execute("""
                    INSERT INTO article_versions
                        (jurisdiction, sr_number, canton, lang, article_num, heading, text,
                         valid_from, status, change_type, source)
                    VALUES ('cantonal', %s, %s, %s, %s, %s, %s, %s, 'in_force', 'initial', 'lexfind')
                    ON CONFLICT DO NOTHING
                """, (sr, canton, lang, anum, art["heading"], art["text"], art["valid_from"]))
                stats["new"] += 1
            elif art["text"] != current[key]["text"]:
                old = current[key]
                diff_summary = _make_diff_summary(old["text"] or "", art["text"])
                cur.execute(
                    "UPDATE article_versions SET valid_to = %s, status = 'superseded' WHERE id = %s",
                    (TODAY, old["id"])
                )
                cur.execute("""
                    INSERT INTO article_versions
                        (jurisdiction, sr_number, canton, lang, article_num, heading, text,
                         valid_from, status, change_type, predecessor_id, diff_summary, source)
                    VALUES ('cantonal', %s, %s, %s, %s, %s, %s, %s, 'in_force', 'modified', %s, %s, 'lexfind')
                    ON CONFLICT DO NOTHING
                """, (sr, canton, lang, anum, art["heading"], art["text"],
                      TODAY, old["id"], diff_summary))
                stats["modified"] += 1
                log.info("  MODIFIED: %s/%s art. %s (%s)", canton, sr, anum, lang)
            else:
                stats["unchanged"] += 1

    pg.commit()
    log.info("Cantonal: %s", stats)
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-scrape", action="store_true", help="Skip Fedlex/LexFind scraping, use existing .db files")
    ap.add_argument("--federal-only", action="store_true")
    ap.add_argument("--cantonal-only", action="store_true")
    args = ap.parse_args()

    _load_env()
    pg = psycopg.connect(get_pg_url(), autocommit=False)
    log.info("Connected to Postgres")

    report = {}
    if not args.cantonal_only:
        report["federal"] = sync_federal(pg, skip_scrape=args.skip_scrape)
    if not args.federal_only:
        report["cantonal"] = sync_cantonal(pg, skip_scrape=args.skip_scrape)

    pg.close()
    log.info("DONE: %s", report)


if __name__ == "__main__":
    main()
