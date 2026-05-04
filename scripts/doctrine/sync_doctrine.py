"""Sync doctrine Markdown files → Postgres doctrine_nodes table.

Reads all .md files under doctrine/, parses YAML frontmatter,
encodes content with BGE-M3, and upserts into doctrine_nodes.

Usage:
    PYTHONPATH=/srv/caselaw .venv/bin/python scripts/doctrine/sync_doctrine.py
"""
from __future__ import annotations

import glob
import logging
import sys
import time
from pathlib import Path

import psycopg
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from search_stack.parag.pg_conn import get_pg_url, _load_env
from search_stack.parag.embedder import load_embedder, _vec_to_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sync-doctrine")

DOCTRINE_DIR = Path(__file__).resolve().parents[2] / "doctrine"


def parse_md(path: Path) -> dict | None:
    """Parse a doctrine .md file with YAML frontmatter."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        log.warning("No frontmatter: %s", path)
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        log.warning("Malformed frontmatter: %s", path)
        return None
    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError as e:
        log.warning("YAML error in %s: %s", path, e)
        return None
    content = parts[2].strip()
    if not meta.get("id") or not content:
        log.warning("Missing id or content: %s", path)
        return None
    meta["content"] = content
    return meta


def main():
    _load_env()
    conn = psycopg.connect(get_pg_url(), autocommit=False)

    # Find all .md files
    files = sorted(DOCTRINE_DIR.rglob("*.md"))
    files = [f for f in files if f.name != "_taxonomy.yaml" and not f.name.startswith("_")]
    log.info("Found %d doctrine files in %s", len(files), DOCTRINE_DIR)

    if not files:
        log.info("No files to sync")
        return

    # Parse all
    nodes = []
    for f in files:
        node = parse_md(f)
        if node:
            nodes.append(node)
    log.info("Parsed %d valid nodes", len(nodes))

    if not nodes:
        return

    # Load embedder
    log.info("Loading BGE-M3 embedder...")
    model = load_embedder(device="cpu")

    # Encode all contents
    texts = [n["content"] for n in nodes]
    log.info("Encoding %d texts...", len(texts))
    t0 = time.monotonic()
    embeddings = model.encode(texts, batch_size=16, show_progress_bar=True,
                              normalize_embeddings=True, convert_to_numpy=True)
    log.info("Encoded in %.1fs", time.monotonic() - t0)

    # Upsert into Postgres
    with conn.cursor() as cur:
        for i, node in enumerate(nodes):
            vec = _vec_to_text(embeddings[i])
            cur.execute("""
                INSERT INTO doctrine_nodes
                    (id, parent_id, level, title_fr, title_de, title_it,
                     content, articles, sr_numbers,
                     keywords_fr, keywords_de, keywords_it,
                     embedding, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, now())
                ON CONFLICT (id) DO UPDATE SET
                    parent_id = EXCLUDED.parent_id,
                    level = EXCLUDED.level,
                    title_fr = EXCLUDED.title_fr,
                    title_de = EXCLUDED.title_de,
                    title_it = EXCLUDED.title_it,
                    content = EXCLUDED.content,
                    articles = EXCLUDED.articles,
                    sr_numbers = EXCLUDED.sr_numbers,
                    keywords_fr = EXCLUDED.keywords_fr,
                    keywords_de = EXCLUDED.keywords_de,
                    keywords_it = EXCLUDED.keywords_it,
                    embedding = EXCLUDED.embedding,
                    updated_at = now()
            """, (
                node["id"],
                node.get("parent"),
                node["id"].count(".") + 1,
                node.get("title_fr", ""),
                node.get("title_de"),
                node.get("title_it"),
                node["content"],
                node.get("articles", []),
                node.get("sr_numbers", []),
                node.get("keywords_fr", []),
                node.get("keywords_de", []),
                node.get("keywords_it", []),
                vec,
            ))
    conn.commit()
    log.info("Upserted %d doctrine nodes to Postgres", len(nodes))

    # Build DiskANN index if enough nodes
    if len(nodes) >= 10:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_doctrine_embedding
                ON doctrine_nodes USING diskann (embedding vector_cosine_ops)
            """)
        conn.commit()
        log.info("DiskANN index created/verified")

    conn.close()
    log.info("DONE")


if __name__ == "__main__":
    main()
