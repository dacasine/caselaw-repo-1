"""Parser for Zurich procedural-style cantonal courts.

Handles:
    zh_obergericht        (~27k, civil/criminal appeals)
    zh_verwaltungsgericht (~12k, admin court)
    zh_handelsgericht     (~5k, commercial court)

NOT handled here (use cantonal_generic):
    zh_sozialversicherungsgericht  (federal-style Sachverhalt + own-line numbering)

Zurich Obergericht-style conventions observed:

1. **Inline considérant numbering**: "1. Die Parteien stehen..." on ONE line
   (number + period + space + content). Federal/BGE uses "1." on its own
   line. This is the dominant difference.

2. **Roman-numeral top-level sections inside Erwägungen**: "I.", "II." for
   major divisions, then Arabic "1.", "2." for sub-points. We treat the
   Arabic numbering as top-level considérants (the Roman divisions are
   structural but not the primary retrieval unit).

3. **No Sachverhalt section**: facts are embedded in the procedural
   preamble (Anklage, Urteil der Vorinstanz, Berufungsanträge) which
   precedes the Erwägungen. No "facts" type section is detected.

4. **Recap of lower court's ruling in preamble**: the header often
   repeats "Es wird erkannt / beschlossen" from the Vorinstanz's
   dispositif, producing a spurious early match for our dispositif
   pattern. We resolve by preferring the LATEST dispositif occurrence
   *after* the Erwägungen marker.

5. **Own-line numbering does still appear** for some sub-levels (e.g.
   when quoting the lower court's dispositive). We match both forms.

Because of (1), we cannot rely on the monotonic-outline filter alone:
inline numbering is more ambiguous and more prone to false positives
from inline enumerations like "1. Mit Verfügung vom …". We mitigate by
requiring the content word after the number to start with a capital
letter (a reasonable proxy for sentence start).
"""

from __future__ import annotations

import re

from search_stack.parag.parsers._common import (
    FEDERAL_SECTION_PATTERNS,
    LETTERED_SUB_RE,
    filter_monotonic,
)
from search_stack.parag.parsers.base import (
    BaseParser,
    Considerant,
    ParsedDecision,
    Section,
    SectionType,
)


#: Inline considérant: "1. Die Parteien..." or "1.1. Das Gericht..."
#: Requires the content to start with a capital letter to avoid matching
#: dates, article refs ("art. 1 Abs."), or broken enumerations.
_ZH_INLINE_TOP_RE = re.compile(r"(?m)^[ \t]*(\d{1,2})\.[ \t]+[A-ZÄÖÜÉÈÀÂÎÔÛÇ]")
_ZH_INLINE_SUB_RE = re.compile(r"(?m)^[ \t]*(\d{1,2}(?:\.\d{1,2})+)\.?[ \t]+[A-ZÄÖÜÉÈÀÂÎÔÛÇ]")
#: Also accept own-line numbering (federal style) for mixed layouts.
_ZH_OWNLINE_TOP_RE = re.compile(r"(?m)^[ \t]*(\d{1,2})\.[ \t]*$")
_ZH_OWNLINE_SUB_RE = re.compile(r"(?m)^[ \t]*(\d{1,2}(?:\.\d{1,2})+)\.?[ \t]*$")


_HANDLED_COURTS: frozenset[str] = frozenset({
    "zh_obergericht",
    "zh_verwaltungsgericht",
    "zh_handelsgericht",
})


def _find_zh_sections(text: str) -> list[Section]:
    """Section marker detection tuned for ZH procedural-style docs.

    Key difference from cantonal_ge: we prefer the LATEST dispositif
    occurrence AFTER the first Erwägungen match, because the header
    often repeats the lower court's dispositif at the top.
    """
    out: list[Section] = []

    # facts — rare in ZH obergericht, but keep detection if present
    for pat in FEDERAL_SECTION_PATTERNS["facts"]:
        m = re.search(pat, text)
        if m is not None:
            out.append(Section(type="facts", start=m.start(), end=m.end(), marker=m.group(0).strip()))
            break

    # considerations — take the first occurrence; "Erwägungen" is rarely repeated
    erw_pos = -1
    for pat in FEDERAL_SECTION_PATTERNS["considerations"]:
        m = re.search(pat, text)
        if m is None:
            continue
        if erw_pos < 0 or m.start() < erw_pos:
            erw_pos = m.start()
            erw_marker = m.group(0).strip()
    if erw_pos >= 0:
        out.append(Section(type="considerations", start=erw_pos, end=erw_pos + 20, marker=erw_marker))

    # dispositif — find all matches, prefer the one AFTER erw_pos (the real
    # ruling), not the one before (recap of prior instance).
    all_disp: list[tuple[int, str]] = []
    for pat in FEDERAL_SECTION_PATTERNS["dispositif"]:
        for m in re.finditer(pat, text):
            all_disp.append((m.start(), m.group(0).strip()))
    all_disp.sort()
    if all_disp:
        if erw_pos >= 0:
            after = [(p, marker) for p, marker in all_disp if p > erw_pos]
            chosen = after[0] if after else all_disp[-1]
        else:
            chosen = all_disp[-1]  # last occurrence (real ruling tends to be at end)
        out.append(Section(type="dispositif", start=chosen[0], end=chosen[0] + 20, marker=chosen[1]))

    return sorted(out, key=lambda s: s.start)


def _slice(text: str, markers: list[Section]) -> list[Section]:
    closed: list[Section] = []
    for i, m in enumerate(markers):
        end = markers[i + 1].start if i + 1 < len(markers) else len(text)
        closed.append(Section(type=m.type, start=m.start, end=end, marker=m.marker))
    return closed


def _parse_zh_considerants(text: str, s_start: int, s_end: int) -> list[Considerant]:
    window = text[s_start:s_end]
    # Combine both numbering forms; union of matches sorted by position.
    all_matches = sorted(
        list(_ZH_INLINE_TOP_RE.finditer(window))
        + list(_ZH_INLINE_SUB_RE.finditer(window))
        + list(_ZH_OWNLINE_TOP_RE.finditer(window))
        + list(_ZH_OWNLINE_SUB_RE.finditer(window)),
        key=lambda m: m.start(),
    )
    if not all_matches:
        return []

    # Deduplicate: if two patterns matched at the same start position, keep only one.
    seen: set[int] = set()
    unique: list = []
    for m in all_matches:
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


class CantonalZHParser(BaseParser):
    name = "cantonal_zh"

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

        markers = _find_zh_sections(full_text)
        sections = _slice(full_text, markers)

        cons_section = next((s for s in sections if s.type == "considerations"), None)
        considerants = (
            _parse_zh_considerants(full_text, cons_section.start, cons_section.end)
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
