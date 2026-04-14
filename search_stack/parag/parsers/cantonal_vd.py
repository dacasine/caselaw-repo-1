"""Parser for Vaud cantonal courts (vd_*).

Handles:
    vd_findinfo    (~75k, Tribunal cantonal findinfo portal)
    vd_gerichte    (~53k, Tribunal cantonal direct scrape)
    vd_omni        (~28k, omnibus Vaud scrape — already well-served by
                    cantonal_generic; routing here as defense-in-depth)

Vaud-specific quirks:

1. **Spaced-out letter markers** (OCR-origin): many decisions render
   section labels with a space between every letter:
       "E n  f a i t  :"        instead of "En fait :"
       "E n  d r o i t  :"      instead of "En droit :"
       "P a r  c e s  m o t i f s"   instead of "Par ces motifs"
   We match both the compact and spaced forms via alternation.

2. **Combined Fait/Droit section**: procedural decisions use a single
   header "En fait et en droit :" (and its spaced variant), merging what
   federal documents would split. We treat this as a *considerations*
   section — the whole body is analytical — so the SAC chunker can still
   operate and the parser remains useful.

3. **Inline considérant numbering**: like Zurich Obergericht, vd_gerichte
   often writes "1. Par ordonnance..." inline. We re-use the inline+own-
   line combined strategy from cantonal_zh.

4. **Page-number noise** ("- 2 -", "1107") inside the body. Not handled
   at the structural level; the SAC summary header will cover context.
"""

from __future__ import annotations

import re

from search_stack.parag.parsers._common import (
    FEDERAL_SECTION_PATTERNS,
    LETTERED_SUB_RE,
    filter_monotonic,
    synthesize_implicit_considerations,
)
from search_stack.parag.parsers.base import (
    BaseParser,
    Considerant,
    ParsedDecision,
    Section,
    SectionType,
)


def _spaced_re(word: str) -> str:
    """Build a regex fragment matching either the compact word or a
    spaced-out version (one or more spaces between every letter).

    >>> _spaced_re("En fait")
    'E[ ]*n[ ]+f[ ]*a[ ]*i[ ]*t'    (conceptually)
    """
    letters = [re.escape(c) for c in word if c != " "]
    # Letters inside a word get [ ]* (can be spaced or not). Between original
    # words (where the source had a space), require at least one space.
    pieces = []
    for ch in word:
        if ch == " ":
            pieces.append(r"[ ]+")
        else:
            pieces.append(re.escape(ch) + r"[ ]*")
    # Drop trailing optional space
    return "".join(pieces).rstrip("[ ]*") + r"[ ]*"


# Vaud-specific section patterns: compact + spaced variants, and combined.
_VD_SECTION_PATTERNS: dict[SectionType, list[str]] = {
    "facts": [
        r"(?mi)^[ \t]*" + _spaced_re("En fait") + r"(?:\s*:)?\s*$",
    ],
    "considerations": [
        r"(?mi)^[ \t]*" + _spaced_re("En droit") + r"(?:\s*:)?\s*$",
        r"(?mi)^[ \t]*" + _spaced_re("En fait et en droit") + r"(?:\s*:)?\s*$",
        r"(?mi)^[ \t]*" + _spaced_re("Considerant") + r"(?:s)?(?:\s*:)?\s*$",
    ],
    "dispositif": [
        r"(?mi)^[ \t]*" + _spaced_re("Par ces motifs") + r"(?:\s*:)?\s*$",
        r"(?mi)^[ \t]*" + _spaced_re("Dispositif") + r"(?:\s*:)?\s*$",
        # Also allow federal fallback patterns for the handful of
        # documents that got ingested with standard federal layout.
        r"(?mi)^[ \t]*Le Tribunal cantonal prononce\b",
        r"(?mi)^[ \t]*La Cour prononce\b",
    ],
}


# Considérant regexes — same as Zurich (inline + own-line).
_VD_INLINE_TOP_RE = re.compile(r"(?m)^[ \t]*(\d{1,2})\.[ \t]+[A-ZÄÖÜÉÈÀÂÎÔÛÇ]")
_VD_INLINE_SUB_RE = re.compile(r"(?m)^[ \t]*(\d{1,2}(?:\.\d{1,2})+)\.?[ \t]+[A-ZÄÖÜÉÈÀÂÎÔÛÇ]")
_VD_OWNLINE_TOP_RE = re.compile(r"(?m)^[ \t]*(\d{1,2})\.[ \t]*$")
_VD_OWNLINE_SUB_RE = re.compile(r"(?m)^[ \t]*(\d{1,2}(?:\.\d{1,2})+)\.?[ \t]*$")


def _find_vd_sections(text: str) -> list[Section]:
    """Try Vaud patterns first; fall back to federal FEDERAL_SECTION_PATTERNS
    if nothing Vaud-specific matched (covers vd_omni and documents in
    federal layout)."""
    collected: list[Section] = []
    for stype, patterns in _VD_SECTION_PATTERNS.items():
        for pat in patterns:
            for m in re.finditer(pat, text):
                collected.append(
                    Section(type=stype, start=m.start(), end=m.end(), marker=m.group(0).strip())
                )

    # If Vaud-specific didn't find anything useful, retry with federal.
    types_found = {s.type for s in collected}
    if not ("facts" in types_found or "considerations" in types_found):
        for stype, patterns in FEDERAL_SECTION_PATTERNS.items():
            for pat in patterns:
                for m in re.finditer(pat, text):
                    collected.append(
                        Section(type=stype, start=m.start(), end=m.end(), marker=m.group(0).strip())
                    )

    first_by_type: dict[SectionType, Section] = {}
    for s in collected:
        if s.type not in first_by_type or s.start < first_by_type[s.type].start:
            first_by_type[s.type] = s
    return sorted(first_by_type.values(), key=lambda s: s.start)


def _slice(text: str, markers: list[Section]) -> list[Section]:
    closed: list[Section] = []
    for i, m in enumerate(markers):
        end = markers[i + 1].start if i + 1 < len(markers) else len(text)
        closed.append(Section(type=m.type, start=m.start, end=end, marker=m.marker))
    return closed


def _parse_vd_considerants(text: str, s_start: int, s_end: int) -> list[Considerant]:
    window = text[s_start:s_end]
    matches = sorted(
        list(_VD_INLINE_TOP_RE.finditer(window))
        + list(_VD_INLINE_SUB_RE.finditer(window))
        + list(_VD_OWNLINE_TOP_RE.finditer(window))
        + list(_VD_OWNLINE_SUB_RE.finditer(window)),
        key=lambda m: m.start(),
    )
    if not matches:
        return []

    seen: set[int] = set()
    unique = []
    for m in matches:
        if m.start() in seen:
            continue
        seen.add(m.start())
        unique.append(m)

    raw: list[Considerant] = []
    for i, m in enumerate(unique):
        number = m.group(1)
        start_abs = s_start + m.start()
        end_abs = s_start + unique[i + 1].start() if i + 1 < len(unique) else s_end
        depth = number.count(".") + 1
        sub_window = text[start_abs:end_abs]
        lettered = [mm.group(1) for mm in LETTERED_SUB_RE.finditer(sub_window)]
        raw.append(
            Considerant(
                number=number,
                start=start_abs,
                end=end_abs,
                depth=depth,
                lettered_subs=lettered,
            )
        )

    filtered = filter_monotonic(raw)
    for i, c in enumerate(filtered):
        c.end = filtered[i + 1].start if i + 1 < len(filtered) else s_end
    return filtered


class CantonalVDParser(BaseParser):
    name = "cantonal_vd"

    def handles(self, court: str) -> bool:
        return court.startswith("vd_")

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

        markers = _find_vd_sections(full_text)
        sections = _slice(full_text, markers)
        sections = synthesize_implicit_considerations(full_text, sections)

        cons_section = next((s for s in sections if s.type == "considerations"), None)
        considerants = (
            _parse_vd_considerants(full_text, cons_section.start, cons_section.end)
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
