"""Shared types and interface for source-aware decision parsers.

Every parser produces the same `ParsedDecision` so the downstream SAC
pipeline (chunking, summary header generation, embedding) is agnostic
of the source.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

SectionType = Literal["header", "facts", "considerations", "dispositif"]


@dataclass
class Section:
    type: SectionType
    start: int
    end: int
    marker: str

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass
class Considerant:
    number: str              # "3", "3.1", "3.1.2"
    start: int
    end: int
    depth: int               # 1 for "3", 2 for "3.1", ...
    lettered_subs: list[str] = field(default_factory=list)  # ["a", "b", ...]

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass
class ParsedDecision:
    decision_id: str
    language: str
    text_length: int
    sections: list[Section]
    considerants: list[Considerant]   # ordered by start
    parser_name: str                  # which parser produced this
    fallback_used: bool = False       # True if a generic fallback was applied

    @property
    def stats(self) -> dict:
        found = {s.type for s in self.sections}
        cons_span = (
            self.considerants[-1].end - self.considerants[0].start
            if self.considerants
            else 0
        )
        return {
            "parser": self.parser_name,
            "fallback_used": self.fallback_used,
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


class BaseParser(ABC):
    """A parser handles one or more `court` values. It either succeeds
    producing a well-formed `ParsedDecision`, or signals to the dispatcher
    that a fallback is needed by returning a result with `fallback_used=True`
    or by raising `ParserRejected`.
    """

    #: Human-readable identifier, used for logging and stats.
    name: str = "base"

    @abstractmethod
    def handles(self, court: str) -> bool: ...

    @abstractmethod
    def parse(self, decision_id: str, language: str, full_text: str) -> ParsedDecision: ...

    def minimally_acceptable(self, parsed: ParsedDecision) -> bool:
        """Heuristic: did this parser find enough structure to be trusted?
        Dispatchers use this to decide whether to try the next fallback.
        Default: at least the considerations section was identified *and*
        at least one considérant was extracted.
        """
        st = parsed.stats
        return st["has_considerations"] and st["n_considerants_total"] >= 1


class ParserRejected(Exception):
    """Raised by a parser to force the dispatcher to try the next fallback."""
