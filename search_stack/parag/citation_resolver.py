"""Canonical citation resolution for Phase 5.

Rule (see memory/project_citation_resolution.md):
    Every statute citation — whether produced by the LLM enrichment
    (`legal_basis`, etc.) or extracted by regex from the chunk text —
    must be canonicalised and, when `statutes.db` is present, resolved
    to its Fedlex SR number. Same principle for case citations against
    the canonical BGE/BGer id format.

This module composes two building blocks that already exist upstream
(`search_stack/reference_extraction.py`) with a thin resolver layer
backed by `statutes.db`.

Usage:
    resolver = CitationResolver(statutes_db_path=Path.home()/".swiss-caselaw"/"statutes.db")
    law_cits, case_cits = resolver.resolve_chunk(
        chunk_id=42,
        chunk_text="...",
        llm_legal_basis=["art. 93 al. 1 let. a LTF", "BGE 128 IV 225"],
        llm_prior_cases=[{"cited_decision": "ATF 120 IV 146", "direction": "confirms"}],
    )
    resolver.store(parag_conn, chunk_id=42, law_cits=law_cits, case_cits=case_cits)
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from search_stack.parag.enrichment import _canonicalise_bge_in_string
from search_stack.reference_extraction import (
    CaseCitation,
    StatuteReference,
    extract_case_citations,
    extract_statute_references,
)


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------

@dataclass
class LawCitation:
    law_abbr: str
    article_num: str | None
    paragraph: str | None
    letter: str | None
    raw_text: str
    normalized: str
    source: str                  # 'llm' | 'regex' | 'both'
    sr_number: str | None = None # populated after resolve_against_statutes
    resolved: bool = False


@dataclass
class CaseCitationRow:
    target_decision_id: str      # canonical — always "BGE 128 IV 225" or docket
    citation_type: str           # 'bge' | 'docket'
    raw_text: str
    source: str                  # 'llm' | 'regex' | 'both'
    direction: str | None = None # from LLM prior_case_treatment, else None


# ---------------------------------------------------------------------------
# Light extractor for LLM-provided strings
# ---------------------------------------------------------------------------

# Match "let. a" / "lit. a" / "ch. 1" / "n. 2" sub-markers for letter/number
# that the upstream regex already captures inside `paragraph`. We separate
# letter for finer storage.
_LETTER_RE = re.compile(r"\b(?:let|lit|Bst|Buchst)\.?\s*([a-z])\b", re.IGNORECASE)


def _split_paragraph_and_letter(paragraph: str | None, raw: str) -> tuple[str | None, str | None]:
    """Heuristically separate paragraph (Abs/al) from letter (lit/let)."""
    letter = None
    m = _LETTER_RE.search(raw)
    if m:
        letter = m.group(1).lower()
    return paragraph, letter


def _llm_string_to_law_citation(raw: str) -> LawCitation | None:
    """Try to parse one LLM-provided string like "art. 93 al. 1 let. a LTF"."""
    refs = extract_statute_references(raw)
    if not refs:
        return None
    r = refs[0]  # first reference in that single string
    paragraph, letter = _split_paragraph_and_letter(r.paragraph, raw)
    return LawCitation(
        law_abbr=r.law_code,
        article_num=r.article,
        paragraph=paragraph,
        letter=letter,
        raw_text=raw,
        normalized=r.normalized,
        source="llm",
    )


def _llm_string_to_case_citation(raw: str, direction: str | None = None) -> CaseCitationRow | None:
    # upstream's BGE_PATTERN only matches the "BGE" prefix; the LLM may
    # output "ATF …" or "DTF …" for the same Recueil Officiel citation.
    # Normalise first so extract_case_citations recognises it as bge.
    canonical_raw = _canonicalise_bge_in_string(raw)
    refs = extract_case_citations(canonical_raw)
    if not refs:
        return None
    r = refs[0]
    return CaseCitationRow(
        target_decision_id=r.normalized,
        citation_type=r.citation_type,
        raw_text=raw,
        source="llm",
        direction=direction,
    )


# ---------------------------------------------------------------------------
# Merger
# ---------------------------------------------------------------------------

def _merge_law(llm: list[LawCitation], regex_refs: list[StatuteReference]) -> list[LawCitation]:
    """Merge LLM and regex extractions by normalized key.
    `source` becomes 'both' when the same citation appears in both.
    """
    out: dict[str, LawCitation] = {}
    for cit in llm:
        out[cit.normalized] = cit
    for r in regex_refs:
        # regex refs may not have letter — inherit from the raw source text
        paragraph, letter = _split_paragraph_and_letter(r.paragraph, r.raw)
        key = r.normalized
        if key in out:
            # seen from LLM → upgrade source to 'both'
            out[key].source = "both"
        else:
            out[key] = LawCitation(
                law_abbr=r.law_code,
                article_num=r.article,
                paragraph=paragraph,
                letter=letter,
                raw_text=r.raw,
                normalized=r.normalized,
                source="regex",
            )
    return list(out.values())


def _merge_case(llm: list[CaseCitationRow],
                regex_refs: list[CaseCitation]) -> list[CaseCitationRow]:
    out: dict[str, CaseCitationRow] = {}
    for cit in llm:
        out[cit.target_decision_id] = cit
    for r in regex_refs:
        key = r.normalized
        if key in out:
            out[key].source = "both"
        else:
            out[key] = CaseCitationRow(
                target_decision_id=r.normalized,
                citation_type=r.citation_type,
                raw_text=r.raw,
                source="regex",
            )
    return list(out.values())


# ---------------------------------------------------------------------------
# Resolver class
# ---------------------------------------------------------------------------

class CitationResolver:
    """Orchestrates extraction + merge + optional statutes.db resolution.

    If `statutes_db_path` points at a file that doesn't exist, the
    resolver still canonicalises but leaves `resolved=False` and
    `sr_number=None`. This lets us run the pipeline before statutes.db
    is built, and back-fill resolutions later.
    """

    def __init__(self, statutes_db_path: Path | None = None) -> None:
        self._db_path = statutes_db_path
        self._db: sqlite3.Connection | None = None
        self._abbr_to_sr: dict[str, str] | None = None

    def _open_db(self) -> sqlite3.Connection | None:
        if self._db is not None:
            return self._db
        if not self._db_path or not self._db_path.exists():
            return None
        try:
            # read-only URI so we never create a 0-byte file if path is odd
            uri = f"file:{self._db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            # Sanity check: the expected schema must be present.
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='laws'"
            ).fetchone()
            if row is None:
                conn.close()
                return None
            self._db = conn
            return self._db
        except sqlite3.Error:
            return None

    def _load_abbr_index(self) -> dict[str, str]:
        if self._abbr_to_sr is not None:
            return self._abbr_to_sr
        db = self._open_db()
        if db is None:
            self._abbr_to_sr = {}
            return self._abbr_to_sr
        idx: dict[str, str] = {}
        try:
            for row in db.execute(
                "SELECT sr_number, abbr_de, abbr_fr, abbr_it FROM laws"
            ).fetchall():
                sr = row["sr_number"]
                for abbr in (row["abbr_de"], row["abbr_fr"], row["abbr_it"]):
                    if abbr:
                        idx[abbr.strip().upper()] = sr
        except sqlite3.OperationalError:
            pass
        self._abbr_to_sr = idx
        return idx

    def _article_exists(self, sr_number: str, article_num: str) -> bool:
        db = self._open_db()
        if db is None:
            return False
        try:
            row = db.execute(
                "SELECT 1 FROM articles WHERE sr_number = ? AND article_num = ? LIMIT 1",
                (sr_number, article_num),
            ).fetchone()
            return row is not None
        except sqlite3.OperationalError:
            return False

    def _resolve_one(self, cit: LawCitation) -> None:
        """Populate sr_number + resolved=True when possible."""
        idx = self._load_abbr_index()
        sr = idx.get(cit.law_abbr.upper())
        if not sr:
            return
        cit.sr_number = sr
        if cit.article_num and self._article_exists(sr, cit.article_num):
            cit.resolved = True
        else:
            # SR known but article not found — keep sr_number, mark unresolved
            cit.resolved = False

    # ---- public API ----------------------------------------------------

    def resolve_chunk(
        self,
        *,
        chunk_text: str,
        llm_legal_basis: list[str] | None = None,
        llm_prior_cases: list[dict] | None = None,
    ) -> tuple[list[LawCitation], list[CaseCitationRow]]:
        """Extract + merge + resolve all citations for one chunk."""
        llm_laws: list[LawCitation] = []
        for s in llm_legal_basis or []:
            if not s:
                continue
            parsed = _llm_string_to_law_citation(s)
            if parsed:
                llm_laws.append(parsed)

        llm_cases: list[CaseCitationRow] = []
        for entry in llm_prior_cases or []:
            s = entry.get("cited_decision") if isinstance(entry, dict) else None
            if not s:
                continue
            d = entry.get("direction") if isinstance(entry, dict) else None
            parsed = _llm_string_to_case_citation(s, direction=d)
            if parsed:
                llm_cases.append(parsed)

        canonical_text = _canonicalise_bge_in_string(chunk_text or "")
        regex_laws = extract_statute_references(canonical_text)
        regex_cases = extract_case_citations(canonical_text)

        laws = _merge_law(llm_laws, regex_laws)
        cases = _merge_case(llm_cases, regex_cases)

        for c in laws:
            self._resolve_one(c)

        return laws, cases

    # ---- persistence ---------------------------------------------------

    @staticmethod
    def store(
        conn: sqlite3.Connection,
        chunk_id: int,
        law_cits: list[LawCitation],
        case_cits: list[CaseCitationRow],
    ) -> None:
        cur = conn.cursor()
        cur.execute("DELETE FROM chunk_law_citations  WHERE chunk_id = ?", (chunk_id,))
        cur.execute("DELETE FROM chunk_case_citations WHERE chunk_id = ?", (chunk_id,))
        if law_cits:
            cur.executemany(
                """
                INSERT INTO chunk_law_citations
                    (chunk_id, sr_number, law_abbr, article_num, paragraph, letter,
                     raw_text, normalized, source, resolved)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (chunk_id, c.sr_number, c.law_abbr, c.article_num,
                     c.paragraph, c.letter, c.raw_text, c.normalized,
                     c.source, int(c.resolved))
                    for c in law_cits
                ],
            )
        if case_cits:
            cur.executemany(
                """
                INSERT INTO chunk_case_citations
                    (chunk_id, target_decision_id, citation_type, raw_text, source, direction)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (chunk_id, c.target_decision_id, c.citation_type,
                     c.raw_text, c.source, c.direction)
                    for c in case_cits
                ],
            )
