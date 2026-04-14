"""Last-resort parser: when no source-specific parser finds structure.

Produces a single `considerations` section spanning the entire text and
no numbered considérants. The downstream SAC chunker will then fall back
to positional / sliding-window chunking, relying on LLM-generated summary
headers to compensate for the lack of structural context.
"""

from __future__ import annotations

from search_stack.parag.parsers.base import BaseParser, ParsedDecision, Section


class FallbackParser(BaseParser):
    name = "_fallback"

    def handles(self, court: str) -> bool:
        # The dispatcher calls this only when every source-specific parser
        # declined or produced an unacceptable result.
        return True

    def parse(self, decision_id: str, language: str, full_text: str) -> ParsedDecision:
        sections: list[Section] = []
        if full_text:
            sections.append(
                Section(
                    type="considerations",
                    start=0,
                    end=len(full_text),
                    marker="(fallback: whole text)",
                )
            )
        return ParsedDecision(
            decision_id=decision_id,
            language=language,
            text_length=len(full_text),
            sections=sections,
            considerants=[],
            parser_name=self.name,
            fallback_used=True,
        )

    def minimally_acceptable(self, parsed: ParsedDecision) -> bool:
        # The fallback is always acceptable — it's the terminal step.
        return True
