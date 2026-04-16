"""Side-by-side comparison of Phase 5 enrichment across multiple providers.

Runs the same 5 arrêts against:
  - Kimi-K2-Instruct via synthetic.new (current default)
  - N Gemini variants via OpenRouter (--models flag)

Reports per model: latency, tokens in/out, schema validity, resolved
citations, plus a qualitative diff on principle_questions count,
doctrine detection, prior_case_treatment count.

Usage:
    .venv/bin/python scripts/parag/benchmark_models_phase5.py \\
        --models google/gemini-2.5-flash google/gemini-2.5-flash-lite

Each model keeps the same schema / prompt / post-processing as
validate_enrichment.py — only the HTTP endpoint and model id differ.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from search_stack.parag.citation_resolver import CitationResolver
from search_stack.parag.enrichment import (
    DecisionContext,
    SYSTEM_PROMPT_FULL,
    build_user_prompt,
    canonicalise_bge,
    validate_full,
)
from search_stack.parag.llm_client import SyntheticClient, _load_env_file

_load_env_file()

# Fixed set of 5 decisions, reused across calls for apples-to-apples comparison.
FIXED_IDS = [
    "bge_BGE_140_II_74",
    "bge_BGE_129_IV_322",
    "bge_BGE_135_V_39",
    "bge_BGE_133_II_409",
    "bge_BGE_138_I_454",
]


def fetch_contexts(parag_db: Path, src_db: Path) -> list[DecisionContext]:
    parag = sqlite3.connect(str(parag_db))
    parag.row_factory = sqlite3.Row
    src = sqlite3.connect(str(src_db))
    src.row_factory = sqlite3.Row
    out: list[DecisionContext] = []
    for did in FIXED_IDS:
        row = src.execute(
            "SELECT court, language, decision_date, chamber, COALESCE(regeste,'') AS regeste, full_text "
            "FROM decisions WHERE decision_id=?",
            (did,),
        ).fetchone()
        if row is None:
            continue
        headers = [
            r[0] for r in parag.execute(
                "SELECT summary FROM chunks WHERE decision_id=? AND summary IS NOT NULL "
                "ORDER BY span_start",
                (did,),
            ).fetchall()
        ]
        disp_row = parag.execute(
            "SELECT cleaned FROM chunks WHERE decision_id=? ORDER BY span_start DESC LIMIT 1",
            (did,),
        ).fetchone()
        out.append(DecisionContext(
            decision_id=did,
            court=row["court"],
            language=row["language"],
            date_iso=row["decision_date"] or "",
            chamber=row["chamber"],
            regeste=row["regeste"],
            full_text=row["full_text"],
            chunk_headers=headers,
            dispositif_text=disp_row[0] if disp_row else None,
        ))
    return out


def call_openrouter(
    model: str,
    system: str,
    user: str,
    *,
    max_tokens: int = 3500,
    temperature: float = 0.1,
    timeout: float = 180.0,
) -> dict:
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY missing from env")
    url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    url += "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/dacasine/caselaw-repo-1",
            "X-Title": "PA-RAG Phase 5 benchmark",
        },
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    dt = time.monotonic() - t0
    data = json.loads(raw.decode("utf-8"))
    msg = data.get("choices", [{}])[0].get("message", {})
    content = (msg.get("content") or "").strip()
    usage = data.get("usage", {})
    return {
        "latency": dt,
        "content": content,
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "model_served": data.get("model", model),
    }


def call_synthetic(model: str, system: str, user: str) -> dict:
    """Adapter so synthetic.new shares the same return shape."""
    client = SyntheticClient(model=model, rate_limit_per_minute=30, quota_aware=True)
    resp = client.chat(system=system, user=user, max_tokens=3500, temperature=0.1)
    return {
        "latency": resp.latency_s,
        "content": resp.content,
        "prompt_tokens": resp.usage.get("prompt_tokens", 0),
        "completion_tokens": resp.usage.get("completion_tokens", 0),
        "model_served": resp.model,
    }


def parse_and_assess(content: str) -> dict:
    """Return a summary dict for easy comparison."""
    raw = content.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"error": f"json: {e}", "raw_head": raw[:200]}

    obj = canonicalise_bge(obj)
    errors = validate_full(obj)
    pqs = obj.get("principle_questions", [])
    trs = obj.get("prior_case_treatment", [])
    doc = obj.get("doctrine_discussion", {})

    return {
        "schema_valid": len(errors) == 0,
        "errors": errors,
        "outcome": obj.get("sort", {}).get("outcome"),
        "stage": obj.get("sort", {}).get("procedural_stage"),
        "n_principle_questions": len(pqs),
        "n_obiter": len(obj.get("obiter_dicta", [])),
        "doctrine_discussed": doc.get("discussed", False),
        "doctrine_authors": len(doc.get("authors_cited", []) or []),
        "leading_case_signal": doc.get("is_leading_case_signal", False),
        "n_prior_treatment": len(trs),
        "obj": obj,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", required=True,
                    help="List of OpenRouter model ids (google/gemini-2.5-flash, etc.)")
    ap.add_argument("--include-synthetic", action="store_true",
                    help="Also run Kimi-K2-Instruct via synthetic.new as baseline")
    args = ap.parse_args()

    src_db = Path.home() / ".swiss-caselaw" / "decisions.db"
    parag_db = Path.home() / ".swiss-caselaw" / "parag_chunks.db"
    contexts = fetch_contexts(parag_db, src_db)
    if not contexts:
        print("No decisions loaded.")
        return

    # Build the (system, user) prompts once per decision
    prompts = []
    for ctx in contexts:
        prompts.append((ctx, SYSTEM_PROMPT_FULL, build_user_prompt(ctx)))

    resolver = CitationResolver(
        statutes_db_path=Path.home() / ".swiss-caselaw" / "statutes.db",
        cantonal_db_path=Path.home() / ".swiss-caselaw" / "cantonal_laws.db",
    )

    # Per-model aggregate
    results: dict[str, dict] = {}

    model_list = list(args.models)
    if args.include_synthetic:
        model_list.insert(0, "SYNTHETIC:hf:moonshotai/Kimi-K2-Instruct-0905")

    for model_id in model_list:
        print(f"\n{'═'*72}")
        print(f"MODEL: {model_id}")
        print(f"{'═'*72}")
        agg = {
            "n_ok": 0, "n_schema_fail": 0, "n_json_fail": 0,
            "latency_sum": 0.0, "prompt_sum": 0, "completion_sum": 0,
            "principle_questions_total": 0, "prior_treatment_total": 0,
            "doctrine_true": 0, "leading_case_true": 0,
            "resolved_laws": 0, "unresolved_laws": 0, "cases_total": 0,
            "per_decision": [],
        }
        for ctx, system, user in prompts:
            try:
                if model_id.startswith("SYNTHETIC:"):
                    resp = call_synthetic(model_id.split(":", 1)[1], system, user)
                else:
                    resp = call_openrouter(model_id, system, user)
            except urllib.error.HTTPError as e:
                print(f"  [{ctx.decision_id}] HTTP {e.code}: {e.read()[:200]}")
                continue
            except Exception as e:
                print(f"  [{ctx.decision_id}] ERROR: {e}")
                continue

            result = parse_and_assess(resp["content"])
            if "error" in result:
                print(f"  [{ctx.decision_id}] JSON PARSE FAIL: {result['error']}")
                print(f"      raw: {result.get('raw_head', '')}")
                agg["n_json_fail"] += 1
                continue

            obj = result["obj"]
            # Run citation resolver
            llm_basis: list[str] = []
            for pq in obj.get("principle_questions", []):
                llm_basis.extend(pq.get("legal_basis", []) or [])
            laws, cases = resolver.resolve_chunk(
                chunk_text=ctx.full_text,
                llm_legal_basis=llm_basis,
                llm_prior_cases=obj.get("prior_case_treatment", []),
                self_decision_id=ctx.decision_id,
            )
            resolved_n = sum(1 for c in laws if c.resolved)

            agg["n_ok" if result["schema_valid"] else "n_schema_fail"] += 1
            agg["latency_sum"] += resp["latency"]
            agg["prompt_sum"] += resp["prompt_tokens"]
            agg["completion_sum"] += resp["completion_tokens"]
            agg["principle_questions_total"] += result["n_principle_questions"]
            agg["prior_treatment_total"] += result["n_prior_treatment"]
            agg["doctrine_true"] += int(result["doctrine_discussed"])
            agg["leading_case_true"] += int(result["leading_case_signal"])
            agg["resolved_laws"] += resolved_n
            agg["unresolved_laws"] += len(laws) - resolved_n
            agg["cases_total"] += len(cases)

            print(f"  [{ctx.decision_id}]  {resp['latency']:>5.1f}s  "
                  f"in={resp['prompt_tokens']:<5} out={resp['completion_tokens']:<5}  "
                  f"valid={result['schema_valid']}  "
                  f"pqs={result['n_principle_questions']}  "
                  f"prior={result['n_prior_treatment']}  "
                  f"doctrine={result['doctrine_discussed']}/{result['doctrine_authors']}a  "
                  f"laws={resolved_n}/{len(laws)}  cases={len(cases)}")

            agg["per_decision"].append({
                "decision_id": ctx.decision_id,
                "result": result,
            })

        results[model_id] = agg

    # Summary table
    print(f"\n{'═'*72}")
    print("SUMMARY")
    print(f"{'═'*72}")
    headers = ["model", "ok", "avg_lat", "tok_in", "tok_out",
               "pqs_tot", "prior_tot", "doc", "lead", "laws_ok", "cases"]
    print(f"  {'MODEL':<45} {'ok/fail':<8} {'avg_lat':>8} {'in_avg':>7} {'out_avg':>7} "
          f"{'PQ':>4} {'PT':>4} {'Doc':>4} {'Lead':>5} {'LwOK':>5} {'Case':>5}")
    for model_id, agg in results.items():
        n = max(agg["n_ok"] + agg["n_schema_fail"], 1)
        short = model_id.replace("SYNTHETIC:hf:moonshotai/", "syn:").replace("google/", "gg:")[:44]
        print(f"  {short:<45} "
              f"{agg['n_ok']}/{agg['n_schema_fail']+agg['n_json_fail']:<6} "
              f"{agg['latency_sum']/n:>7.1f}s "
              f"{agg['prompt_sum']/n:>7.0f} "
              f"{agg['completion_sum']/n:>7.0f} "
              f"{agg['principle_questions_total']:>4} "
              f"{agg['prior_treatment_total']:>4} "
              f"{agg['doctrine_true']:>4} "
              f"{agg['leading_case_true']:>5} "
              f"{agg['resolved_laws']:>5} "
              f"{agg['cases_total']:>5}")


if __name__ == "__main__":
    main()
