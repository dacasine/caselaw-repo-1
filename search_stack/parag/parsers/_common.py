"""Shared parsing primitives used by multiple source-specific parsers.

These helpers are patterns-as-data: callers pass their own regexes and
section markers. Keeps each parser module small and focused on what
differs for its source.
"""

from __future__ import annotations

import re

from search_stack.parag.parsers.base import Considerant, Section, SectionType


def find_section_markers(
    text: str,
    patterns: dict[SectionType, list[str]],
) -> list[Section]:
    candidates: list[Section] = []
    for stype, pats in patterns.items():
        for pat in pats:
            for m in re.finditer(pat, text):
                candidates.append(
                    Section(type=stype, start=m.start(), end=m.end(), marker=m.group(0).strip())
                )
    first_by_type: dict[SectionType, Section] = {}
    for s in candidates:
        if s.type not in first_by_type or s.start < first_by_type[s.type].start:
            first_by_type[s.type] = s
    return sorted(first_by_type.values(), key=lambda s: s.start)


def slice_sections(text: str, markers: list[Section]) -> list[Section]:
    closed: list[Section] = []
    for i, m in enumerate(markers):
        end = markers[i + 1].start if i + 1 < len(markers) else len(text)
        closed.append(Section(type=m.type, start=m.start, end=end, marker=m.marker))
    return closed


def synthesize_implicit_considerations(
    text: str, sections: list[Section]
) -> list[Section]:
    """Some federal courts (TAF/bvger, TPF/bstger) omit the Erwägungen label
    and jump directly from Sachverhalt to "Demnach erkennt". In that case,
    `slice_sections` makes the facts section absorb the considérants too.

    Locate the first top-level considérant marker (e.g. "1.") inside the
    facts span and split facts there, creating an implicit considerations
    section between the split and the dispositif.
    """
    types = {s.type for s in sections}
    if "considerations" in types or "facts" not in types or "dispositif" not in types:
        return sections

    facts = next(s for s in sections if s.type == "facts")
    disp = next(s for s in sections if s.type == "dispositif")

    # Look for the first "N." on its own line within the facts region that
    # starts at a plausible considérant (N in 1..9).
    first_cons_re = re.compile(r"(?m)^[ \t]*([1-9])\.[ \t]*$")
    probe = text[facts.start:disp.start]
    m = first_cons_re.search(probe)
    if m is None:
        return sections
    split_at = facts.start + m.start()
    # Guard: the "1." must be past the marker itself. Marker words are at
    # most ~15 chars ("Sachverhalt:\n" = 12). 5 rejects only impossible
    # matches that collide with the marker start.
    if split_at <= facts.start + 5:
        return sections

    new_facts = Section(
        type="facts",
        start=facts.start,
        end=split_at,
        marker=facts.marker,
    )
    synthetic = Section(
        type="considerations",
        start=split_at,
        end=disp.start,
        marker="(implicit: first numbered considérant found at split point)",
    )
    return sorted(
        [s for s in sections if s.type not in ("facts",)] + [new_facts, synthetic],
        key=lambda s: s.start,
    )


def is_valid_next(cur: tuple[int, ...], nxt: tuple[int, ...]) -> bool:
    """Legal next step in a numbered outline walk.

      (a) deeper:    cur prefix of nxt, tail digits all 1       e.g. (3,) → (3,1)
      (b) sibling:   same depth, same parent, value +1          e.g. (3,1) → (3,2)
      (c) shallower: truncate cur to nxt depth, value +1 there  e.g. (3,3,1) → (3,4)
    """
    if len(nxt) > len(cur):
        return nxt[: len(cur)] == cur and all(v == 1 for v in nxt[len(cur):])
    if len(nxt) == len(cur):
        return nxt[:-1] == cur[:-1] and nxt[-1] == cur[-1] + 1
    return nxt[:-1] == cur[: len(nxt) - 1] and nxt[-1] == cur[len(nxt) - 1] + 1


def filter_monotonic(candidates: list[Considerant]) -> list[Considerant]:
    """Drop candidates that break the monotonic outline walk (typically
    quoted statute paragraph numbers)."""
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
        if is_valid_next(cursor, parts):
            accepted.append(c)
            cursor = parts
    return accepted


def parse_considerants(
    text: str,
    section_start: int,
    section_end: int,
    top_re: re.Pattern[str],
    sub_re: re.Pattern[str],
    lettered_re: re.Pattern[str],
) -> list[Considerant]:
    window = text[section_start:section_end]
    raw_matches = sorted(
        list(top_re.finditer(window)) + list(sub_re.finditer(window)),
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
        lettered = [mm.group(1) for mm in lettered_re.finditer(sub_window)]
        raw_candidates.append(
            Considerant(
                number=number,
                start=start_abs,
                end=end_abs,
                depth=depth,
                lettered_subs=lettered,
            )
        )

    filtered = filter_monotonic(raw_candidates)
    for i, c in enumerate(filtered):
        c.end = filtered[i + 1].start if i + 1 < len(filtered) else section_end
    return filtered


#: Federal tribunal section markers — shared by BGE modern, BGer, TAF, TPF.
FEDERAL_SECTION_PATTERNS: dict[SectionType, list[str]] = {
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
        # Cantonal / specialised courts: "Das Einzelgericht zieht in Erwägung",
        # "Die Kammer zieht in Erwägung", "Das Gericht erwägt", etc.
        r"(?mi)^[ \t]*(?:Das|Die|Der)\s+\S+\s+(?:zieht\s+in\s+Erw(?:ä|ae)gung|erw(?:ä|ae)gt)\s*:?\s*$",
        r"(?mi)^[ \t]*Die\s+\S+kammer\s+erw(?:ä|ae)gt\s*:?\s*$",
    ],
    "dispositif": [
        # Federal (BGE / BGer / TAF / TPF)
        r"(?mi)^[ \t]*Demnach erkennt\b",
        r"(?mi)^[ \t]*Demnach beschliesst\b",
        r"(?mi)^[ \t]*Par ces motifs\b",
        r"(?mi)^[ \t]*Per questi motivi\b",
        r"(?mi)^[ \t]*Il Tribunale federale pronuncia\b",
        r"(?mi)^[ \t]*Das Bundesgericht erkennt\b",
        r"(?mi)^[ \t]*Le Tribunal f(?:é|e)d(?:é|e)ral prononce\b",
        # Cantonal — generic court verdict openers
        r"(?mi)^[ \t]*Das Gericht erkennt\b",
        r"(?mi)^[ \t]*Das Gericht beschliesst\b",
        r"(?mi)^[ \t]*Das (?:Ober|Verwaltungs|Sozialversicherungs|Handels|Kantons|Appellations|Steuerrekurs)gericht erkennt\b",
        r"(?mi)^[ \t]*Das (?:Ober|Verwaltungs|Sozialversicherungs|Handels|Kantons|Appellations)gericht beschliesst\b",
        r"(?mi)^[ \t]*Demgem(?:ä|ae)ss erkennt\b",
        r"(?mi)^[ \t]*Demgem(?:ä|ae)ss beschliesst\b",
        # Zurich convention — passive form
        r"(?mi)^[ \t]*Es wird erkannt\b",
        r"(?mi)^[ \t]*Es wird beschlossen\b",
        r"(?mi)^[ \t]*Es wird verf(?:ü|ue)gt\b",
        # Generic headers
        r"(?mi)^[ \t]*Dispositiv\s*:?\s*$",
        r"(?mi)^[ \t]*Dispositif\s*:?\s*$",
        # Italian cantonal
        r"(?mi)^[ \t]*Il (?:Tribunale|Giudice|Pretore) (?:cantonale|di|amministrativo|delle assicurazioni)",
        r"(?mi)^[ \t]*(?:Il|La) (?:Pretore|Corte) (?:pronuncia|decide|statuisce)\b",
    ],
}

#: Letter sub-items — same convention on every federal source.
LETTERED_SUB_RE = re.compile(r"(?m)^[ \t]*([a-z])\s*\)\s+")
