"""Summary-Augmented Chunking (SAC) builder.

Given a ParsedDecision (from parsers.parse()), produce a list of Chunk
objects ready for embedding. For each chunk we decide whether to request
a 1-sentence summary header from the LLM based on three heuristics:

    1. Skip stubs          — chunks whose *cleaned* body is under
                              STUB_MAX_CHARS (100). These are considérant
                              shells (e.g. "1." on a bare line) whose real
                              content lives in sub-levels 1.1, 1.2...
    2. Skip self-sufficient — chunks at SELF_SUFFICIENT_MIN_CHARS (500)
                              or more that already contain a complete
                              narrative (≥ 2 sentences). The SAC summary
                              would be redundant.
    3. Otherwise            — ask the LLM for a summary header.

Summaries are requested in *batches* (SAC_BATCH_SIZE, default 5) to
amortise model warm-up / reasoning overhead. The batch prompt asks the
model for strict per-chunk markers ([C1], [C2]...) that we parse back.
Parse failures fall back to single-chunk calls for the affected items.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from search_stack.parag.llm_client import SyntheticClient
from search_stack.parag.parsers._common import clean_chunk_text
from search_stack.parag.parsers.base import Considerant, ParsedDecision


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

STUB_MAX_CHARS = 100
SELF_SUFFICIENT_MIN_CHARS = 500
SELF_SUFFICIENT_MIN_SENTENCES = 2
# Batch doubled from 5 → 10 after model-benchmark validation: Kimi-K2-Instruct
# uses ~250 tokens for 5 summaries, so a batch of 10 fits comfortably under
# LLM_MAX_TOKENS_PER_BATCH. Non-reasoning models have stable sub-10s latency
# so we can amortise more chunks per call.
SAC_BATCH_SIZE = 10
LLM_MAX_TOKENS_PER_BATCH = 4000      # headroom for 10 summaries × ~300 tokens
SUMMARY_MAX_CHARS = 320              # truncate if the model rambles


_LANG_LABELS = {
    "de": "allemand",
    "fr": "français",
    "it": "italien",
    "rm": "français",   # romansh unusual, fall back to FR summaries
}

# Heuristic sentence counter — permissive but OK for triage.
_SENTENCE_END_RE = re.compile(r"[.!?][ \t\n]")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Chunk:
    decision_id: str
    court: str
    language: str
    considerant_number: str       # "3", "3.1", "3.1.2" (or "implicit" for prose)
    depth: int
    span_start: int               # offset in the source decision full_text
    span_end: int
    raw_length: int               # length of the raw span (including noise)
    cleaned: str                  # text after clean_chunk_text — what goes to the embedder
    reason_skipped: str | None = None   # None = we'll embed it
    summary: str | None = None    # None = not requested / not yet generated
    summary_source: str | None = None   # "stub" | "self_sufficient" | "llm" | "error"


@dataclass
class BuildStats:
    chunks_total: int = 0
    stubs: int = 0                # filtered out pre-LLM
    self_sufficient: int = 0      # kept, no LLM call
    summarized: int = 0           # kept, LLM summary attached
    llm_calls: int = 0
    llm_errors: int = 0
    llm_latency_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    fallback_used: int = 0


# ---------------------------------------------------------------------------
# Triage helpers
# ---------------------------------------------------------------------------

def _count_sentences(text: str) -> int:
    return len(_SENTENCE_END_RE.findall(text)) + (1 if text and text[-1] in ".!?" else 0)


def _triage(cleaned: str) -> tuple[bool, str | None]:
    """Return (needs_llm_summary, skip_reason)."""
    n = len(cleaned)
    if n < STUB_MAX_CHARS:
        return False, "stub"
    if n >= SELF_SUFFICIENT_MIN_CHARS and _count_sentences(cleaned) >= SELF_SUFFICIENT_MIN_SENTENCES:
        return False, "self_sufficient"
    return True, None


# ---------------------------------------------------------------------------
# Considérant → Chunk
# ---------------------------------------------------------------------------

def build_chunks_from_parsed(
    parsed: ParsedDecision,
    court: str,
    full_text: str,
) -> list[Chunk]:
    """Materialise chunks from a ParsedDecision, without calling the LLM."""
    out: list[Chunk] = []

    # When there are numbered considérants, each is a chunk. When there are
    # none but a considerations section exists (prose-only or fallback),
    # treat the whole section as one chunk (the SAC chunker will split
    # further by paragraph when we later implement the long-considérant
    # recursive splitter).
    if parsed.considerants:
        for c in parsed.considerants:
            raw = full_text[c.start:c.end]
            cleaned = clean_chunk_text(raw)
            needs_llm, reason = _triage(cleaned)
            out.append(
                Chunk(
                    decision_id=parsed.decision_id,
                    court=court,
                    language=parsed.language,
                    considerant_number=c.number,
                    depth=c.depth,
                    span_start=c.start,
                    span_end=c.end,
                    raw_length=len(raw),
                    cleaned=cleaned,
                    reason_skipped=None if needs_llm else reason,
                )
            )
    else:
        cons = next((s for s in parsed.sections if s.type == "considerations"), None)
        if cons is not None:
            raw = full_text[cons.start:cons.end]
            cleaned = clean_chunk_text(raw)
            needs_llm, reason = _triage(cleaned)
            out.append(
                Chunk(
                    decision_id=parsed.decision_id,
                    court=court,
                    language=parsed.language,
                    considerant_number="implicit",
                    depth=0,
                    span_start=cons.start,
                    span_end=cons.end,
                    raw_length=len(raw),
                    cleaned=cleaned,
                    reason_skipped=None if needs_llm else reason,
                )
            )

    # Mark chunks whose triage skipped the LLM with a summary_source label.
    for ch in out:
        if ch.reason_skipped is not None:
            ch.summary_source = ch.reason_skipped

    return out


# ---------------------------------------------------------------------------
# Batch prompt construction + response parsing
# ---------------------------------------------------------------------------

_BATCH_SYSTEM = (
    "Tu es un juriste spécialiste du droit suisse. Pour chaque considérant "
    "fourni (identifié par [C1], [C2], ...), tu écris UNE SEULE phrase "
    "concise (15-35 mots) en {lang} qui contextualise le considérant dans "
    "l'arrêt : la question juridique traitée, la partie concernée et/ou la "
    "conclusion du considérant.\n\n"
    "RÈGLES STRICTES DE FORMAT :\n"
    "- Une ligne par considérant.\n"
    "- Chaque ligne commence EXACTEMENT par le marqueur [Ck] suivi d'un "
    "espace puis de la phrase, sans guillemets.\n"
    "- Ne produis rien d'autre : pas d'introduction, pas de conclusion, pas "
    "de numérotation additionnelle.\n"
    "- Respecte strictement l'ordre des [Ck] du prompt utilisateur."
)


def _build_user_batch(
    decision_id: str,
    court: str,
    regeste: str,
    chunks: list[Chunk],
) -> str:
    head = [
        f"Arrêt : {decision_id}  (cour : {court})",
        f"Regeste : {regeste.strip()[:600] if regeste else '(non disponible)'}",
        "",
        f"{len(chunks)} considérants à résumer :",
        "",
    ]
    body: list[str] = []
    for i, ch in enumerate(chunks, start=1):
        body.append(f"[C{i}] considérant {ch.considerant_number} (depth={ch.depth})")
        # Truncate each chunk to keep the batch prompt under 6-8k chars total.
        body.append(ch.cleaned[:1500])
        body.append("")
    return "\n".join(head + body)


_RESPONSE_MARKER_RE = re.compile(r"^\s*\[C(\d+)\]\s*(.+?)\s*$", re.MULTILINE)


def _parse_batch_response(text: str, n_chunks: int) -> dict[int, str]:
    """Return a map 1-indexed chunk position → summary text. Missing entries
    indicate the model skipped that chunk (caller will single-retry)."""
    out: dict[int, str] = {}
    for m in _RESPONSE_MARKER_RE.finditer(text):
        idx = int(m.group(1))
        summary = m.group(2).strip().strip('"').strip("'")
        if summary and 1 <= idx <= n_chunks:
            out[idx] = summary[:SUMMARY_MAX_CHARS]
    return out


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def generate_summaries(
    chunks: list[Chunk],
    client: SyntheticClient,
    *,
    regeste: str = "",
    batch_size: int = SAC_BATCH_SIZE,
    stats: BuildStats | None = None,
) -> BuildStats:
    """Attach .summary to every chunk whose triage keeps it in-scope.
    Mutates chunks in place. Returns BuildStats."""
    stats = stats or BuildStats()
    stats.chunks_total += len(chunks)
    stats.stubs += sum(1 for c in chunks if c.summary_source == "stub")
    stats.self_sufficient += sum(1 for c in chunks if c.summary_source == "self_sufficient")

    to_summarize = [c for c in chunks if c.reason_skipped is None]
    if not to_summarize:
        return stats

    # Process in batches of the same decision/language.
    decision_id = to_summarize[0].decision_id
    court = to_summarize[0].court
    lang = to_summarize[0].language
    lang_label = _LANG_LABELS.get(lang, "français")
    sys_prompt = _BATCH_SYSTEM.replace("{lang}", lang_label)

    for start in range(0, len(to_summarize), batch_size):
        batch = to_summarize[start:start + batch_size]
        user = _build_user_batch(decision_id, court, regeste, batch)
        try:
            resp = client.chat(
                system=sys_prompt,
                user=user,
                max_tokens=LLM_MAX_TOKENS_PER_BATCH,
                temperature=0.2,
            )
            stats.llm_calls += 1
            stats.llm_latency_s += resp.latency_s
            stats.prompt_tokens += resp.usage.get("prompt_tokens", 0)
            stats.completion_tokens += resp.usage.get("completion_tokens", 0)
            if resp.fallback_used:
                stats.fallback_used += 1
        except Exception as exc:
            for ch in batch:
                ch.summary = None
                ch.summary_source = "error"
            stats.llm_errors += 1
            continue

        parsed = _parse_batch_response(resp.content, len(batch))
        for i, ch in enumerate(batch, start=1):
            summary = parsed.get(i)
            if summary:
                ch.summary = summary
                ch.summary_source = "llm"
                stats.summarized += 1
            else:
                ch.summary = None
                ch.summary_source = "error"
                stats.llm_errors += 1

    return stats
