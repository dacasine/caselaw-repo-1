"""Parser for Swiss federal courts other than BGE (Recueil Officiel).

Handles:
    bger     — Tribunal fédéral, unpublished decisions (~175k)
    bvger    — Tribunal administratif fédéral / TAF (~92k)
    bstger   — Tribunal pénal fédéral / TPF (~11k)
    bpatger  — Tribunal fédéral des brevets / FPC (smaller)

Shared conventions:
    - multilingual headers (Bundesgericht / Tribunal fédéral / ...)
    - "Sachverhalt:" / "Faits :" / "In fatto :" section label (usually)
    - considérants "N." top-level + "N.M" or "N.M." sub-levels
    - "Demnach erkennt..." / "Par ces motifs..." dispositif

Quirk of TAF and TPF: no explicit "Erwägungen" label — they jump straight
from Sachverhalt to the numbered considérants. We synthesize the implicit
considerations section between facts and dispositif.

Secondary format observed in TAF expedited decisions: a "que... / que..."
chain with no numbered considérants. These fall back to _fallback.
"""

from __future__ import annotations

import re

from search_stack.parag.parsers._common import (
    FEDERAL_SECTION_PATTERNS,
    LETTERED_SUB_RE,
    find_section_markers,
    parse_considerants,
    slice_sections,
    synthesize_implicit_considerations,
)
from search_stack.parag.parsers.base import BaseParser, ParsedDecision


# Sub-levels: trailing period optional (BGer uses "1.1.", some TAF drops it).
_TOP_RE = re.compile(r"(?m)^[ \t]*(\d+)\.[ \t]*$")
_SUB_RE = re.compile(r"(?m)^[ \t]*(\d+(?:\.\d+)+)\.?[ \t]*$")

_HANDLED_COURTS = frozenset({"bger", "bvger", "bstger", "bpatger"})


class FederalParser(BaseParser):
    name = "federal"

    def handles(self, court: str) -> bool:
        return court in _HANDLED_COURTS

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

        markers = find_section_markers(full_text, FEDERAL_SECTION_PATTERNS)
        sections = slice_sections(full_text, markers)
        sections = synthesize_implicit_considerations(full_text, sections)

        cons_section = next((s for s in sections if s.type == "considerations"), None)
        considerants = (
            parse_considerants(
                full_text,
                cons_section.start,
                cons_section.end,
                _TOP_RE,
                _SUB_RE,
                LETTERED_SUB_RE,
            )
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
