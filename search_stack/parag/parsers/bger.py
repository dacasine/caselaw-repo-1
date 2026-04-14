"""Parser for Tribunal fédéral unpublished decisions ("BGer").

Format is very close to modern BGE — same multilingual headers, same
section markers (Sachverhalt / Erwägungen / Demnach erkennt). The one
divergence observed is that **sub-levels carry a trailing period**:

    BGE modern:  "1.1" / "3.3.1"       (no trailing period)
    BGer:        "1.1." / "3.3.1."     (trailing period)

Top-level considérants use the same "1." form.

The dispatcher routes `court == "bger"` to this parser. If we ever see
a BGer document that has neither "Sachverhalt" nor "Erwägungen" markers,
the dispatcher falls back to `_fallback` as usual.
"""

from __future__ import annotations

import re

from search_stack.parag.parsers._common import (
    FEDERAL_SECTION_PATTERNS,
    LETTERED_SUB_RE,
    find_section_markers,
    parse_considerants,
    slice_sections,
)
from search_stack.parag.parsers.base import BaseParser, ParsedDecision


# Top-level identical to BGE modern.
_TOP_RE = re.compile(r"(?m)^[ \t]*(\d+)\.[ \t]*$")
# Sub-levels: trailing period optional to cover both conventions in case
# a single document mixes them (rare but observed on long BGer arrêts).
_SUB_RE = re.compile(r"(?m)^[ \t]*(\d+(?:\.\d+)+)\.?[ \t]*$")


class BGerParser(BaseParser):
    name = "bger"

    def handles(self, court: str) -> bool:
        return court == "bger"

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
