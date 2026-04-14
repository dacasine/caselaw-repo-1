"""Source-aware parser registry and dispatcher.

Usage:
    from search_stack.parag.parsers import parse

    result = parse(decision_id="bge_BGE_148_II_336",
                   court="bge",
                   language="fr",
                   full_text=txt)

Resolution order for a given `court`:
    1. every parser whose `handles(court)` returns True, in registration order
    2. if none produce a `minimally_acceptable` result, fall back to
       FallbackParser (never rejects).
"""

from __future__ import annotations

from search_stack.parag.parsers._fallback import FallbackParser
from search_stack.parag.parsers.base import BaseParser, ParsedDecision, ParserRejected
from search_stack.parag.parsers.bge_modern import BGEModernParser


# Registration order matters: source-specific parsers first, fallback last.
_REGISTRY: list[BaseParser] = [
    BGEModernParser(),
]

_FALLBACK: BaseParser = FallbackParser()


def parse(
    decision_id: str,
    court: str,
    language: str,
    full_text: str,
) -> ParsedDecision:
    """Dispatch to the best-matching parser for this court, with cascading
    fallback to the generic parser if no source-specific parser succeeds.
    """
    for parser in _REGISTRY:
        if not parser.handles(court):
            continue
        try:
            result = parser.parse(decision_id, language, full_text)
        except ParserRejected:
            continue
        if parser.minimally_acceptable(result):
            return result

    # No source-specific parser succeeded — use the fallback.
    return _FALLBACK.parse(decision_id, language, full_text)


def register(parser: BaseParser) -> None:
    """Plug-in hook for adding new parsers at runtime (tests, notebooks)."""
    _REGISTRY.append(parser)
