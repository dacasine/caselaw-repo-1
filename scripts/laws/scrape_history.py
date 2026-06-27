"""Scrape historical consolidations from Fedlex for article version tracking.

For each law, fetches ALL consolidation dates via SPARQL, downloads the XML
for each consolidation, parses articles, and diffs consecutive versions to
populate article_versions with accurate valid_from/valid_to dates.

Usage:
    PYTHONPATH=/srv/caselaw .venv/bin/python scripts/laws/scrape_history.py [--sr 220] [--top N] [--skip-existing]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from search_stack.parag.pg_conn import get_pg_url, _load_env

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("scrape-history")

REQUEST_DELAY = 0.5  # be polite to Fedlex


# ---------------------------------------------------------------------------
# SPARQL helpers
# ---------------------------------------------------------------------------

def sparql_query(query: str, timeout: int = 60) -> list[dict]:
    url = "https://fedlex.data.admin.ch/sparqlendpoint?query=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"Accept": "application/sparql-results+json"})
    resp = urllib.request.urlopen(req, timeout=timeout)
    data = json.loads(resp.read())
    return [
        {k: v["value"] for k, v in row.items()}
        for row in data["results"]["bindings"]
    ]


def get_consolidation_dates(sr_number: str) -> list[dict]:
    """Get all unique consolidation dates + URIs for a law."""
    query = f"""
    PREFIX jolux: <http://data.legilux.public.lu/resource/ontology/jolux#>
    SELECT DISTINCT ?date ?consolidation WHERE {{
      ?work a jolux:ConsolidationAbstract .
      ?work jolux:historicalLegalId '{sr_number}' .
      ?consolidation jolux:isMemberOf ?work .
      ?consolidation jolux:dateApplicability ?date .
    }}
    ORDER BY ?date
    """
    rows = sparql_query(query, timeout=120)
    # Dedupe by date (keep first URI per date)
    seen = {}
    for r in rows:
        d = r["date"][:10]  # YYYY-MM-DD
        if d not in seen:
            seen[d] = r["consolidation"]
    return [{"date": d, "uri": u} for d, u in sorted(seen.items())]


# ---------------------------------------------------------------------------
# XML article parsing (reuse build_statutes_db logic)
# ---------------------------------------------------------------------------

AKN_NS = {"akn": "http://docs.oasis-open.org/legaldocml/ns/akn/3.0"}


LANG_URIS = {
    "de": "http://publications.europa.eu/resource/authority/language/DEU",
    "fr": "http://publications.europa.eu/resource/authority/language/FRA",
    "it": "http://publications.europa.eu/resource/authority/language/ITA",
}


def resolve_xml_url(consolidation_uri: str, lang: str = "de") -> str | None:
    """Resolve the actual XML download URL for a consolidation via SPARQL."""
    lang_uri = LANG_URIS.get(lang, LANG_URIS["de"])
    query = f"""
    PREFIX jolux: <http://data.legilux.public.lu/resource/ontology/jolux#>
    SELECT ?url WHERE {{
      <{consolidation_uri}> jolux:isRealizedBy ?expr .
      ?expr jolux:language <{lang_uri}> .
      ?expr jolux:isEmbodiedBy ?manif .
      ?manif jolux:userFormat <https://fedlex.data.admin.ch/vocabulary/user-format/xml> .
      ?manif jolux:isExemplifiedBy ?url .
    }}
    LIMIT 1
    """
    try:
        rows = sparql_query(query, timeout=30)
        return rows[0]["url"] if rows else None
    except Exception:
        return None


def download_xml(consolidation_uri: str, lang: str = "de") -> str | None:
    """Download Akoma Ntoso XML for a consolidation (resolves URL via SPARQL)."""
    xml_url = resolve_xml_url(consolidation_uri, lang)
    if not xml_url:
        return None
    try:
        req = urllib.request.Request(xml_url, headers={"User-Agent": "PA-RAG/1.0"})
        resp = urllib.request.urlopen(req, timeout=60)
        return resp.read().decode("utf-8")
    except Exception:
        return None


def parse_articles_from_xml(xml_text: str) -> dict[str, dict]:
    """Parse articles from Akoma Ntoso XML. Returns {article_num: {heading, text, footnote}}."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}

    articles = {}
    for art in root.iter("{http://docs.oasis-open.org/legaldocml/ns/akn/3.0}article"):
        # Extract article number from eId
        eid = art.get("eId", "")
        # eId format: "art_41" or "art_321bis"
        anum = eid.replace("art_", "").replace("_", " ") if eid.startswith("art_") else ""
        if not anum:
            continue

        # Heading (marginal note)
        heading_el = art.find(".//{http://docs.oasis-open.org/legaldocml/ns/akn/3.0}heading")
        heading = heading_el.text.strip() if heading_el is not None and heading_el.text else ""

        # Text content
        texts = []
        for p in art.iter("{http://docs.oasis-open.org/legaldocml/ns/akn/3.0}p"):
            t = "".join(p.itertext()).strip()
            if t:
                texts.append(t)

        # Footnotes (amendment refs)
        footnotes = []
        for note in art.iter("{http://docs.oasis-open.org/legaldocml/ns/akn/3.0}authorialNote"):
            t = "".join(note.itertext()).strip()
            if t:
                footnotes.append(t)

        text = "\n".join(texts)
        if text:
            articles[anum] = {
                "heading": heading,
                "text": text,
                "footnote": "; ".join(footnotes) if footnotes else None,
            }
    return articles


# ---------------------------------------------------------------------------
# Diff and version tracking
# ---------------------------------------------------------------------------

def process_law_history(
    pg: psycopg.Connection, sr_number: str, lang: str = "de",
):
    """Fetch all historical versions for a law and populate article_versions."""
    consolidations = get_consolidation_dates(sr_number)
    if not consolidations:
        log.warning("No consolidations found for SR %s", sr_number)
        return {"status": "no_consolidations"}

    log.info("SR %s: %d consolidations (%s → %s)",
             sr_number, len(consolidations),
             consolidations[0]["date"], consolidations[-1]["date"])

    # Check what we already have
    existing_dates = set(
        r[0].isoformat() for r in pg.execute(
            "SELECT DISTINCT valid_from FROM article_versions "
            "WHERE jurisdiction = 'federal' AND sr_number = %s AND lang = %s",
            (sr_number, lang)
        ).fetchall()
    )

    prev_articles: dict[str, dict] = {}
    stats = {"versions_processed": 0, "articles_added": 0, "articles_modified": 0, "articles_abrogated": 0, "skipped": 0}

    for i, cons in enumerate(consolidations):
        cons_date = cons["date"]

        if cons_date in existing_dates:
            # Already processed — but we need prev_articles for the next diff
            # Load from DB
            rows = pg.execute(
                "SELECT article_num, text, heading FROM article_versions "
                "WHERE jurisdiction='federal' AND sr_number=%s AND lang=%s AND valid_from=%s",
                (sr_number, lang, cons_date)
            ).fetchall()
            prev_articles = {r[0]: {"text": r[1], "heading": r[2]} for r in rows}
            stats["skipped"] += 1
            continue

        # Download and parse XML
        xml = download_xml(cons["uri"], lang)
        if not xml:
            # Try fallback languages
            for fallback in ["fr", "it"]:
                if fallback != lang:
                    xml = download_xml(cons["uri"], fallback)
                    if xml:
                        break
        if not xml:
            log.warning("  %s: no XML available for %s", sr_number, cons_date)
            time.sleep(REQUEST_DELAY)
            continue

        curr_articles = parse_articles_from_xml(xml)
        if not curr_articles:
            time.sleep(REQUEST_DELAY)
            continue

        is_first = (i == 0) or not prev_articles

        with pg.cursor() as cur:
            # New or modified articles
            for anum, art in curr_articles.items():
                prev = prev_articles.get(anum)
                if prev is None:
                    # New article
                    change_type = "initial" if is_first else "added"
                    cur.execute("""
                        INSERT INTO article_versions
                            (jurisdiction, sr_number, lang, article_num, heading, text, footnote,
                             valid_from, status, change_type, source)
                        VALUES ('federal', %s, %s, %s, %s, %s, %s, %s, 'in_force', %s, 'fedlex')
                        ON CONFLICT DO NOTHING
                    """, (sr_number, lang, anum, art["heading"], art["text"],
                          art.get("footnote"), cons_date, change_type))
                    stats["articles_added"] += 1
                elif art["text"] != prev.get("text", "") or art["heading"] != prev.get("heading", ""):
                    # Modified — close old version, open new
                    cur.execute("""
                        UPDATE article_versions SET valid_to = %s, status = 'superseded'
                        WHERE jurisdiction = 'federal' AND sr_number = %s AND lang = %s
                          AND article_num = %s AND status = 'in_force'
                    """, (cons_date, sr_number, lang, anum))
                    cur.execute("""
                        INSERT INTO article_versions
                            (jurisdiction, sr_number, lang, article_num, heading, text, footnote,
                             valid_from, status, change_type, source)
                        VALUES ('federal', %s, %s, %s, %s, %s, %s, %s, 'in_force', 'modified', 'fedlex')
                        ON CONFLICT DO NOTHING
                    """, (sr_number, lang, anum, art["heading"], art["text"],
                          art.get("footnote"), cons_date))
                    stats["articles_modified"] += 1

            # Abrogated articles (in prev but not in curr)
            if not is_first:
                for anum in prev_articles:
                    if anum not in curr_articles:
                        cur.execute("""
                            UPDATE article_versions SET valid_to = %s, status = 'abrogated', change_type = 'abrogated'
                            WHERE jurisdiction = 'federal' AND sr_number = %s AND lang = %s
                              AND article_num = %s AND status = 'in_force'
                        """, (cons_date, sr_number, lang, anum))
                        stats["articles_abrogated"] += 1

        pg.commit()
        prev_articles = {a: {"text": v["text"], "heading": v["heading"]} for a, v in curr_articles.items()}
        stats["versions_processed"] += 1
        time.sleep(REQUEST_DELAY)

    log.info("SR %s: %s", sr_number, stats)
    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sr", type=str, help="Process a single SR number")
    ap.add_argument("--top", type=int, default=20, help="Process top N most-cited laws")
    ap.add_argument("--lang", default="de", help="Language to scrape (de/fr/it)")
    ap.add_argument("--skip-existing", action="store_true", default=True)
    args = ap.parse_args()

    _load_env()
    pg = psycopg.connect(get_pg_url(), autocommit=False)

    if args.sr:
        # Single law
        process_law_history(pg, args.sr, lang=args.lang)
    else:
        # Top N most-cited laws from decision_statutes
        rows = pg.execute("""
            SELECT sr_number, count(*) AS n FROM decision_statutes
            WHERE sr_number ~ '^[0-9]'
            GROUP BY sr_number ORDER BY n DESC LIMIT %s
        """, (args.top,)).fetchall()

        if not rows:
            # Fallback: top federal laws by article count
            rows = pg.execute("""
                SELECT sr_number, count(*) AS n FROM articles_federal
                GROUP BY sr_number ORDER BY n DESC LIMIT %s
            """, (args.top,)).fetchall()

        log.info("Processing %d laws", len(rows))
        for sr, n in rows:
            log.info("=== SR %s (%d citations/articles) ===", sr, n)
            try:
                process_law_history(pg, sr, lang=args.lang)
            except Exception as e:
                log.error("Failed SR %s: %s", sr, e)

    pg.close()
    log.info("DONE")


if __name__ == "__main__":
    main()
