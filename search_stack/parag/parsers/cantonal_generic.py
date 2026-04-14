"""Generic cantonal court parser.

Swiss cantonal decisions vary widely in layout, but a large majority still
use a recognizable facts / considerations / dispositif structure with
multilingual markers inherited from federal practice:

    DE : Sachverhalt / Erwägungen / (Demnach) erkennt
    FR : EN FAIT / EN DROIT / Par ces motifs        (often uppercase)
    IT : in fatto / in diritto / Il Tribunale...     (often lowercase, with comma)

This parser tries the federal patterns first (case-insensitive, so EN FAIT
matches), synthesizes an implicit considerations section if needed, and
supports sub-level considérants with optional trailing period (since many
cantons write "1.1." like TF/TAF).

When the minimally_acceptable bar is not met for a given document, the
dispatcher falls through to _fallback as usual.

Cantons/courts currently routed here (selected by volume, can be extended):
    ge_*          Genève (all chambers)
    vd_*          Vaud (findinfo, gerichte, omni)
    ti_gerichte   Tessin
    zh_*          Zurich (except obergericht which has atypical layout)
    bl_gerichte, gr_gerichte, fr_gerichte, be_*
    ne_gerichte, so_gerichte, bs_appellationsgericht, sg_*
    ar_gerichte, ag_gerichte, sz_gerichte, vs_gerichte, lu_gerichte

Sources that need a *specific* parser (not handled here):
    zh_obergericht                 — atypical (Rechtsbegehren/Verfügung headers)
    ch_vb                          — admin regulatory acts, not judicial decisions
    bpatger                        — Federal Patent Court specialised layout

Specific overrides (zh_obergericht etc.) will be added as separate parser
modules in later iterations if their volume justifies the effort.
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


# Same regexes as federal parser — cantons follow TF conventions for numbering.
_TOP_RE = re.compile(r"(?m)^[ \t]*(\d+)\.[ \t]*$")
_SUB_RE = re.compile(r"(?m)^[ \t]*(\d+(?:\.\d+)+)\.?[ \t]*$")


# Curated list of cantonal courts routed to this generic parser.
# Prefixes (e.g. "ge_") match any court starting with that prefix.
_HANDLED_PREFIXES: tuple[str, ...] = (
    "ge_",
    "vd_",
    "ti_",
    "zh_sozialversicherungs",   # ZH social insurance — closer to federal style
    "zh_verwaltungsgericht",    # ZH admin court
    "zh_handelsgericht",        # ZH commercial court
    "bl_",
    "gr_",
    "fr_",
    "be_",
    "ne_",
    "so_",
    "bs_",
    "sg_",
    "ar_",
    "ag_",
    "sz_",
    "vs_",
    "lu_",
    "gl_",
    "nw_",
    "ow_",
    "ur_",
    "ai_",
    "tg_",
    "ju_",
    "sh_",
    "zg_",
)

# Explicit courts NOT handled here (atypical layouts, need specific parsers).
_EXCLUDED_COURTS: frozenset[str] = frozenset({
    "zh_obergericht",
    "ch_vb",
    "bpatger",
})


class CantonalGenericParser(BaseParser):
    name = "cantonal_generic"

    def handles(self, court: str) -> bool:
        if court in _EXCLUDED_COURTS:
            return False
        return any(court.startswith(p) for p in _HANDLED_PREFIXES)

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
