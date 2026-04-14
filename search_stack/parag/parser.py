"""Structural parser for Swiss court decisions (POC, Phase 3).

Detects three high-level sections (facts / considerations / dispositif) and,
within the considerations section, numbered considérants with nested
sub-levels (3., 3.1, 3.1.2) plus lettered sub-items (a), b)).

Scope of this POC: BGE (ATF) decisions in DE/FR/IT. Cantonal/unstructured
decisions are out of scope and handled in a later iteration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

SectionType = Literal["header", "facts", "considerations", "dispositif"]


_SECTION_PATTERNS: dict[SectionType, list[str]] = {
    "facts": [
        r"(?mi)^[ \t]*Sachverhalt\b",
        r"(?mi)^[ \t]*En fait\b",
        r"(?mi)^[ \t]*Faits\b",
        r"(?mi)^[ \t]*In fatto\b",
        r"(?mi)^[ \t]*Fatti\b",
    ],
    "considerations": [
        r"(?mi)^[ \t]*Erw(?:ä|ae)gungen\b",
        r"(?mi)^[ \t]*Consid(?:é|e)rant(?:s)?\b",
        r"(?mi)^[ \t]*En droit\b",
        r"(?mi)^[ \t]*Considerando\b",
        r"(?mi)^[ \t]*In diritto\b",
        r"(?mi)^[ \t]*Diritto\b",
    ],
    "dispositif": [
        r"(?mi)^[ \t]*Demnach erkennt\b",
        r"(?mi)^[ \t]*Demnach beschliesst\b",
        r"(?mi)^[ \t]*Par ces motifs\b",
        r"(?mi)^[ \t]*Per questi motivi\b",
        r"(?mi)^[ \t]*Il Tribunale federale pronuncia\b",
        r"(?mi)^[ \t]*Das Bundesgericht erkennt\b",
        r"(?mi)^[ \t]*Le Tribunal f(?:é|e)d(?:é|e)ral prononce\b",
    ],
}

# Modern BGE convention: considérants are ALWAYS on their own line.
# Top-level: "1." (digit + period, end of line).
# Sub-level: "1.1", "3.3.1" (multi-part number, no trailing period, end of line).
# This strict form rejects common false positives from quoted statute text:
#   "1" / "2" / "3" alone on a line    (statute paragraphs, no period)
#   "2. im Dienste der..."              (statute sub-items, inline content)
_CONSIDERANT_TOP_RE = re.compile(r"(?m)^[ \t]*(\d+)\.[ \t]*$")
_CONSIDERANT_SUB_RE = re.compile(r"(?m)^[ \t]*(\d+(?:\.\d+)+)[ \t]*$")
_LETTERED_SUB_RE = re.compile(r"(?m)^[ \t]*([a-z])\s*\)\s+")


@dataclass
class Section:
    type: SectionType
    start: int
    end: int
    marker: str  # the exact matched marker text

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass
class Considerant:
    number: str              # "3" or "3.1" or "3.1.2"
    start: int               # absolute offset in full_text
    end: int                 # absolute offset (exclusive)
    depth: int               # 1 for "3", 2 for "3.1", 3 for "3.1.2"
    lettered_subs: list[str] = field(default_factory=list)  # e.g. ["a", "b"]

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass
class ParsedDecision:
    decision_id: str
    language: str
    text_length: int
    sections: list[Section]
    considerants: list[Considerant]  # flat list, ordered by start

    # coverage stats
    @property
    def stats(self) -> dict:
        found = {s.type for s in self.sections}
        # Considérants tile the considerations section contiguously by
        # construction (each ends at the next one's start). Total coverage
        # is therefore from first.start to last.end.
        if self.considerants:
            cons_span = self.considerants[-1].end - self.considerants[0].start
        else:
            cons_span = 0
        return {
            "has_facts": "facts" in found,
            "has_considerations": "considerations" in found,
            "has_dispositif": "dispositif" in found,
            "n_sections": len(self.sections),
            "n_considerants_top": sum(1 for c in self.considerants if c.depth == 1),
            "n_considerants_total": len(self.considerants),
            "max_depth": max((c.depth for c in self.considerants), default=0),
            "considerant_coverage_ratio": (
                cons_span / self.text_length if self.text_length else 0.0
            ),
        }


def _find_section_markers(text: str) -> list[Section]:
    """Find every section marker occurrence, keeping only the *first* match
    for each section type (BGE often repeats "Regeste" or multilingual headers)."""
    candidates: list[Section] = []
    for stype, patterns in _SECTION_PATTERNS.items():
        for pat in patterns:
            for m in re.finditer(pat, text):
                candidates.append(
                    Section(type=stype, start=m.start(), end=m.end(), marker=m.group(0).strip())
                )

    # Keep earliest occurrence per type (first "Sachverhalt" etc.)
    first_by_type: dict[SectionType, Section] = {}
    for s in candidates:
        if s.type not in first_by_type or s.start < first_by_type[s.type].start:
            first_by_type[s.type] = s

    return sorted(first_by_type.values(), key=lambda s: s.start)


def _slice_sections(text: str, markers: list[Section]) -> list[Section]:
    """Close section boundaries: each section runs until the next marker or EOF."""
    closed: list[Section] = []
    for i, m in enumerate(markers):
        end = markers[i + 1].start if i + 1 < len(markers) else len(text)
        closed.append(Section(type=m.type, start=m.start, end=end, marker=m.marker))
    return closed


def _is_valid_next(cur: tuple[int, ...], nxt: tuple[int, ...]) -> bool:
    """True if `nxt` is a legal next step in a numbered outline starting at `cur`.

    Three legal moves:
      (a) deeper:    cur is a prefix of nxt, and the new tail digits are all 1
                     e.g. (3,) → (3, 1) or (3, 1, 1)
      (b) sibling:   same depth, same parent, value increments by 1
                     e.g. (3, 1) → (3, 2)
      (c) shallower: truncate cur to nxt's depth, same parent, value +1
                     e.g. (3, 3, 1) → (3, 4) or (3, 3, 1) → (4,)
    """
    if len(nxt) > len(cur):
        return nxt[: len(cur)] == cur and all(v == 1 for v in nxt[len(cur):])
    if len(nxt) == len(cur):
        return nxt[:-1] == cur[:-1] and nxt[-1] == cur[-1] + 1
    # shallower
    return nxt[:-1] == cur[: len(nxt) - 1] and nxt[-1] == cur[len(nxt) - 1] + 1


def _filter_monotonic(candidates: list[Considerant]) -> list[Considerant]:
    """Keep only candidates that form a valid monotonic outline walk.

    This filters out paragraph/sub-item numbers of statutes quoted inside a
    considérant (e.g. `Art. 1a AHVG` quoted in extenso with its paragraphs
    "1", "2", "3" and items "1.", "2.", "3.").
    """
    accepted: list[Considerant] = []
    cursor: tuple[int, ...] | None = None

    for c in candidates:
        try:
            parts = tuple(int(p) for p in c.number.split("."))
        except ValueError:
            continue
        # sanity bound: real considérants rarely exceed 30 in any component
        if any(p > 50 for p in parts):
            continue

        if cursor is None:
            # first accepted candidate — BGE excerpts can start at high numbers
            # (e.g. "Aus den Erwägungen: 14.") so we just accept it as anchor.
            accepted.append(c)
            cursor = parts
            continue

        if _is_valid_next(cursor, parts):
            accepted.append(c)
            cursor = parts

    return accepted


def _parse_considerants(text: str, section_start: int, section_end: int) -> list[Considerant]:
    """Find numbered considérants within a [section_start, section_end) window."""
    window = text[section_start:section_end]
    raw_matches = sorted(
        list(_CONSIDERANT_TOP_RE.finditer(window))
        + list(_CONSIDERANT_SUB_RE.finditer(window)),
        key=lambda m: m.start(),
    )
    if not raw_matches:
        return []

    # Build raw candidates with absolute offsets; close each at the next match.
    raw_candidates: list[Considerant] = []
    for i, m in enumerate(raw_matches):
        number = m.group(1)
        start_abs = section_start + m.start()
        end_abs = (
            section_start + raw_matches[i + 1].start()
            if i + 1 < len(raw_matches)
            else section_end
        )
        depth = number.count(".") + 1
        sub_window = text[start_abs:end_abs]
        lettered = [mm.group(1) for mm in _LETTERED_SUB_RE.finditer(sub_window)]
        raw_candidates.append(
            Considerant(
                number=number,
                start=start_abs,
                end=end_abs,
                depth=depth,
                lettered_subs=lettered,
            )
        )

    # Filter to a coherent outline walk.
    filtered = _filter_monotonic(raw_candidates)

    # Re-close end offsets after filtering so each considérant spans up to the
    # next *accepted* considérant (the raw end may stop at a rejected candidate).
    for i, c in enumerate(filtered):
        c.end = filtered[i + 1].start if i + 1 < len(filtered) else section_end
    return filtered


def parse(decision_id: str, language: str, full_text: str) -> ParsedDecision:
    """Entry point: returns a structured view of the decision text."""
    if not full_text:
        return ParsedDecision(
            decision_id=decision_id,
            language=language,
            text_length=0,
            sections=[],
            considerants=[],
        )

    markers = _find_section_markers(full_text)
    sections = _slice_sections(full_text, markers)

    cons_section = next((s for s in sections if s.type == "considerations"), None)
    considerants = (
        _parse_considerants(full_text, cons_section.start, cons_section.end)
        if cons_section
        else []
    )

    return ParsedDecision(
        decision_id=decision_id,
        language=language,
        text_length=len(full_text),
        sections=sections,
        considerants=considerants,
    )
