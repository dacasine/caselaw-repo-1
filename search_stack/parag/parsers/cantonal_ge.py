"""Parser for Geneva cantonal courts (ge_*).

Geneva-specific conventions observed:

1. **Uppercase section labels**: "EN FAIT", "EN DROIT" (vs federal "En fait").
   Our case-insensitive regex already handles this, but see point 3.

2. **Parenthesis numbering** for considérants: "1)", "2)", "3)" followed by
   content. Federal/BGE uses "1." (period). Need a separate regex.

3. **Table-of-contents noise at document start**: many Geneva decisions
   begin with a short header block that *literally lists* "En fait" and
   "En droit" as a mini-summary of the arrêt's structure, before the
   actual case text. The case-insensitive match would lock onto these
   lowercase mentions. We skip matches that produce absurdly short
   section spans (< 200 chars) and retry from further down.

4. **Page-number noise**: "- 2/18 -" style page breaks inside the text.
   Not handled at the structural level; they'll just be included in the
   chunk text and filtered by the SAC summary header if relevant.

Courts handled: any "ge_*" except specifically excluded below.
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


# Geneva considérants use parenthesis-number format on their own line:
#   "1)"  top-level
#   "1.1)" sub-level (observed but rare; keep flexible)
# We also keep the federal "1." / "1.1" patterns as a second chance since
# some Geneva tribunals (notably Chambre des assurances sociales) follow
# the federal convention.
_GE_TOP_RE = re.compile(r"(?m)^[ \t]*(\d+)\)[ \t]*$")
_GE_SUB_RE = re.compile(r"(?m)^[ \t]*(\d+(?:\.\d+)+)\)[ \t]*$")
_FEDERAL_TOP_RE = re.compile(r"(?m)^[ \t]*(\d+)\.[ \t]*$")
_FEDERAL_SUB_RE = re.compile(r"(?m)^[ \t]*(\d+(?:\.\d+)+)\.?[ \t]*$")

#: Minimum span (in characters) for a detected section to be considered
#: real (rather than a TOC entry at the top of the document).
_MIN_REAL_SECTION_SPAN = 200


def _robust_find_section_markers(text: str) -> list[Section]:
    """Variant of find_section_markers that skips short intro sections
    likely belonging to a table of contents at the top of the document."""
    per_type: dict[SectionType, list[re.Match[str]]] = {
        stype: [] for stype in FEDERAL_SECTION_PATTERNS
    }
    for stype, patterns in FEDERAL_SECTION_PATTERNS.items():
        for pat in patterns:
            per_type[stype].extend(re.finditer(pat, text))

    # For each type, pick the first occurrence whose span to the *next*
    # type's first occurrence is at least _MIN_REAL_SECTION_SPAN.
    # Simpler heuristic: pick the LAST occurrence if there are multiple
    # and they're all within the first 1500 chars — strong signal of a TOC.
    out: list[Section] = []
    for stype, matches in per_type.items():
        if not matches:
            continue
        matches_sorted = sorted(matches, key=lambda m: m.start())
        # Drop matches that appear before position 800 *only if* another
        # match of the same type exists further down — TOC signature.
        if len(matches_sorted) >= 2 and matches_sorted[0].start() < 800:
            picked = matches_sorted[1]
        else:
            picked = matches_sorted[0]
        out.append(
            Section(type=stype, start=picked.start(), end=picked.end(), marker=picked.group(0).strip())
        )
    return sorted(out, key=lambda s: s.start)


def _slice_with_min_span(
    text: str, markers: list[Section]
) -> list[Section]:
    """Slice section ends at next marker, dropping any with span too small
    to be credible (also a TOC guard)."""
    closed: list[Section] = []
    for i, m in enumerate(markers):
        end = markers[i + 1].start if i + 1 < len(markers) else len(text)
        span = end - m.start
        if span < _MIN_REAL_SECTION_SPAN:
            # This section is too short — probably a TOC entry.
            continue
        closed.append(Section(type=m.type, start=m.start, end=end, marker=m.marker))
    return closed


def _parse_considerants_ge(text: str, s_start: int, s_end: int) -> list[Considerant]:
    window = text[s_start:s_end]
    # Try Geneva parenthesis form first; if nothing found, fall back to federal.
    matches = sorted(
        list(_GE_TOP_RE.finditer(window)) + list(_GE_SUB_RE.finditer(window)),
        key=lambda m: m.start(),
    )
    if not matches:
        matches = sorted(
            list(_FEDERAL_TOP_RE.finditer(window))
            + list(_FEDERAL_SUB_RE.finditer(window)),
            key=lambda m: m.start(),
        )
    if not matches:
        return []

    raw: list[Considerant] = []
    for i, m in enumerate(matches):
        number = m.group(1)
        start_abs = s_start + m.start()
        end_abs = s_start + matches[i + 1].start() if i + 1 < len(matches) else s_end
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


class CantonalGEParser(BaseParser):
    name = "cantonal_ge"

    def handles(self, court: str) -> bool:
        return court.startswith("ge_")

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

        markers = _robust_find_section_markers(full_text)
        sections = _slice_with_min_span(full_text, markers)
        sections = synthesize_implicit_considerations(full_text, sections)

        cons_section = next((s for s in sections if s.type == "considerations"), None)
        considerants = (
            _parse_considerants_ge(full_text, cons_section.start, cons_section.end)
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
