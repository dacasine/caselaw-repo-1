"""Parser for modern BGE / ATF decisions (post-1960 convention).

Format hallmarks:
    - multilingual header repeated up to three times (DE/FR/IT)
    - "Sachverhalt" / "En fait" / "In fatto" section label
    - "Erwägungen" / "Considérant en droit" / "In diritto" section label
    - top-level considérants numbered "N." on their own line
    - sub-levels "N.M", "N.M.P" on their own line
    - optional "Demnach erkennt das Bundesgericht" / "Par ces motifs" dispositif

Out of scope: pre-1960 BGE (no structural labels, use bge_legacy).
"""

from __future__ import annotations

import re

from search_stack.parag.parsers.base import (
    BaseParser,
    Considerant,
    ParsedDecision,
    Section,
    SectionType,
)


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

# Strict forms — modern BGE has considérants on their own line.
_CONSIDERANT_TOP_RE = re.compile(r"(?m)^[ \t]*(\d+)\.[ \t]*$")
_CONSIDERANT_SUB_RE = re.compile(r"(?m)^[ \t]*(\d+(?:\.\d+)+)[ \t]*$")
_LETTERED_SUB_RE = re.compile(r"(?m)^[ \t]*([a-z])\s*\)\s+")


def _find_section_markers(text: str) -> list[Section]:
    candidates: list[Section] = []
    for stype, patterns in _SECTION_PATTERNS.items():
        for pat in patterns:
            for m in re.finditer(pat, text):
                candidates.append(
                    Section(type=stype, start=m.start(), end=m.end(), marker=m.group(0).strip())
                )
    # Keep earliest occurrence per type.
    first_by_type: dict[SectionType, Section] = {}
    for s in candidates:
        if s.type not in first_by_type or s.start < first_by_type[s.type].start:
            first_by_type[s.type] = s
    return sorted(first_by_type.values(), key=lambda s: s.start)


def _slice_sections(text: str, markers: list[Section]) -> list[Section]:
    closed: list[Section] = []
    for i, m in enumerate(markers):
        end = markers[i + 1].start if i + 1 < len(markers) else len(text)
        closed.append(Section(type=m.type, start=m.start, end=end, marker=m.marker))
    return closed


def _is_valid_next(cur: tuple[int, ...], nxt: tuple[int, ...]) -> bool:
    """Legal outline transitions — see parser design notes."""
    if len(nxt) > len(cur):
        return nxt[: len(cur)] == cur and all(v == 1 for v in nxt[len(cur):])
    if len(nxt) == len(cur):
        return nxt[:-1] == cur[:-1] and nxt[-1] == cur[-1] + 1
    # shallower
    return nxt[:-1] == cur[: len(nxt) - 1] and nxt[-1] == cur[len(nxt) - 1] + 1


def _filter_monotonic(candidates: list[Considerant]) -> list[Considerant]:
    accepted: list[Considerant] = []
    cursor: tuple[int, ...] | None = None
    for c in candidates:
        try:
            parts = tuple(int(p) for p in c.number.split("."))
        except ValueError:
            continue
        if any(p > 50 for p in parts):
            continue
        if cursor is None:
            accepted.append(c)
            cursor = parts
            continue
        if _is_valid_next(cursor, parts):
            accepted.append(c)
            cursor = parts
    return accepted


def _parse_considerants(text: str, section_start: int, section_end: int) -> list[Considerant]:
    window = text[section_start:section_end]
    raw_matches = sorted(
        list(_CONSIDERANT_TOP_RE.finditer(window))
        + list(_CONSIDERANT_SUB_RE.finditer(window)),
        key=lambda m: m.start(),
    )
    if not raw_matches:
        return []

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

    filtered = _filter_monotonic(raw_candidates)
    for i, c in enumerate(filtered):
        c.end = filtered[i + 1].start if i + 1 < len(filtered) else section_end
    return filtered


class BGEModernParser(BaseParser):
    name = "bge_modern"

    def handles(self, court: str) -> bool:
        return court == "bge"

    def parse(self, decision_id: str, language: str, full_text: str) -> ParsedDecision:
        if not full_text:
            return ParsedDecision(
                decision_id=decision_id,
                language=language,
                text_length=0,
                sections=[],
                considerants=[],
                parser_name=self.name,
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
            parser_name=self.name,
        )
