"""Validate the Phase 5 enrichment prompt on 5 ATF before scaling.

Takes 5 random BGE decisions (that have SAC chunks in parag_chunks.db),
builds the context, calls the LLM once per decision, parses JSON, runs
validate_full(), prints a human-readable report.

Usage:
    .venv/bin/python scripts/parag/validate_enrichment.py [--n 5] [--light]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from db_schema_parag import DEFAULT_PARAG_DB
from search_stack.parag.citation_resolver import CitationResolver
from search_stack.parag.enrichment import (
    DecisionContext,
    SYSTEM_PROMPT_FULL,
    SYSTEM_PROMPT_LIGHT,
    build_user_prompt,
    canonicalise_bge,
    validate_full,
)
from search_stack.parag.llm_client import SyntheticClient


def pick_decisions(parag_db: Path, n: int) -> list[dict]:
    parag = sqlite3.connect(str(parag_db))
    parag.row_factory = sqlite3.Row
    src = sqlite3.connect(str(Path.home() / ".swiss-caselaw" / "decisions.db"))
    src.row_factory = sqlite3.Row

    # Decisions with at least 3 chunks in our DB, from BGE modern
    dids = parag.execute(
        "SELECT decision_id, court, language FROM chunks "
        "WHERE decision_id LIKE 'bge_BGE_%' "
        "GROUP BY decision_id HAVING COUNT(*) >= 3 "
        "ORDER BY RANDOM() LIMIT ?",
        (n,),
    ).fetchall()
    out = []
    for row in dids:
        did = row["decision_id"]
        full_row = src.execute(
            "SELECT decision_date, chamber, COALESCE(regeste,'') AS regeste, full_text "
            "FROM decisions WHERE decision_id = ?",
            (did,),
        ).fetchone()
        if full_row is None:
            continue

        # Gather SAC summary headers for this decision
        headers = [
            h[0] for h in parag.execute(
                "SELECT summary FROM chunks WHERE decision_id = ? AND summary IS NOT NULL "
                "ORDER BY span_start",
                (did,),
            ).fetchall()
        ]

        # Try to extract the dispositif chunk text (cheap heuristic: last chunk)
        disp = parag.execute(
            "SELECT cleaned FROM chunks WHERE decision_id = ? ORDER BY span_start DESC LIMIT 1",
            (did,),
        ).fetchone()

        out.append({
            "decision_id": did,
            "court": row["court"],
            "language": row["language"],
            "date_iso": full_row["decision_date"] or "",
            "chamber": full_row["chamber"],
            "regeste": full_row["regeste"],
            "full_text": full_row["full_text"],
            "chunk_headers": headers,
            "dispositif_text": disp[0] if disp else None,
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--light", action="store_true",
                    help="Use the lighter BGer schema instead of full BGE")
    ap.add_argument("--parag-db", type=Path, default=DEFAULT_PARAG_DB)
    args = ap.parse_args()

    decs = pick_decisions(args.parag_db, args.n)
    if not decs:
        print("No decisions found")
        return

    client = SyntheticClient(rate_limit_per_minute=10)
    system = SYSTEM_PROMPT_LIGHT if args.light else SYSTEM_PROMPT_FULL
    resolver = CitationResolver(
        statutes_db_path=Path.home() / ".swiss-caselaw" / "statutes.db",
        cantonal_db_path=Path.home() / ".swiss-caselaw" / "cantonal_laws.db",
    )

    for i, d in enumerate(decs, 1):
        ctx = DecisionContext(**d)
        user = build_user_prompt(ctx, light=args.light)
        print(f"\n{'═'*70}")
        print(f"[{i}/{len(decs)}]  {d['decision_id']}  [{d['language']}]  "
              f"len={len(d['full_text'])}  headers={len(d['chunk_headers'])}")
        print(f"{'─'*70}")

        try:
            resp = client.chat(system=system, user=user,
                               max_tokens=3500, temperature=0.1)
        except Exception as exc:
            print(f"  LLM ERROR: {exc}")
            continue

        print(f"  model={resp.model} latency={resp.latency_s:.1f}s "
              f"in={resp.usage.get('prompt_tokens', 0)} "
              f"out={resp.usage.get('completion_tokens', 0)}")

        raw = resp.content.strip()
        # Strip common markdown wrappers if the model didn't obey
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]

        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"  JSON PARSE FAIL: {exc}")
            print(f"  RAW[:400] = {raw[:400]!r}")
            continue

        # Safety net: force ATF/DTF → BGE so citations line up with our
        # canonical decision_id format.
        obj = canonicalise_bge(obj)

        errs = validate_full(obj) if not args.light else []
        if errs:
            print(f"  VALIDATION ERRORS: {errs}")
        else:
            print(f"  ✓ schema valid")

        # Compact view for humans
        print(f"\n  sort: {obj.get('sort', {})}")
        pqs = obj.get("principle_questions", [])
        print(f"  principle_questions: {len(pqs)}")
        for j, q in enumerate(pqs):
            print(f"    [{j+1}] Q: {q.get('question', '')[:120]}")
            print(f"         R: {q.get('ratio', '')[:160]}")
            if q.get("legal_basis"):
                print(f"         basis: {q['legal_basis']}")
        obs = obj.get("obiter_dicta", [])
        if obs:
            print(f"  obiter_dicta ({len(obs)}):")
            for ob in obs[:3]:
                print(f"    · {ob[:140]}")
        doc = obj.get("doctrine_discussion", {})
        if doc.get("discussed"):
            print(f"  doctrine: authors={doc.get('authors_cited', [])}  "
                  f"leading_signal={doc.get('is_leading_case_signal', False)}")
            for p in doc.get("positions_weighed", [])[:3]:
                print(f"    · {p[:140]}")
        trs = obj.get("prior_case_treatment", [])
        if trs:
            print(f"  prior_case_treatment ({len(trs)}):")
            for t in trs[:5]:
                print(f"    · {t.get('cited_decision', '?')} → {t.get('direction', '?')}")

        # Canonicalise + extract citations (resolver works even without statutes.db)
        llm_basis: list[str] = []
        for pq in pqs:
            llm_basis.extend(pq.get("legal_basis", []) or [])
        llm_basis.extend(obj.get("legal_basis_main", []) or [])  # light variant

        laws, cases = resolver.resolve_chunk(
            chunk_text=d["full_text"],
            llm_legal_basis=llm_basis,
            llm_prior_cases=trs,
        )
        resolved_n = sum(1 for c in laws if c.resolved)
        print(f"\n  citations: {len(laws)} laws ({resolved_n} resolved), {len(cases)} cases")
        for c in laws[:8]:
            marker = "OK" if c.resolved else "--"
            print(f"    [{marker}] {c.normalized:<28} sr={c.sr_number or '-'}  src={c.source}")
        for c in cases[:6]:
            print(f"    > {c.target_decision_id:<25} {c.citation_type:<7} src={c.source}  dir={c.direction or '-'}")


if __name__ == "__main__":
    main()
