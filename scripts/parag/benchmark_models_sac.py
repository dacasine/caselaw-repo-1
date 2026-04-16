"""Benchmark SAC summary quality across models.

Picks 3 decisions whose chunks have already been Kimi-summarised (so we
have a reference), re-runs the same SAC batch prompt against N candidate
models, compares:
  - latency
  - tokens (prompt/completion)
  - number of parseable summaries (schema)
  - qualitative preview of each summary

Usage:
    .venv/bin/python scripts/parag/benchmark_models_sac.py \\
        --models google/gemini-2.0-flash-001 \\
                 google/gemini-2.5-flash-lite \\
        --include-synthetic
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

from search_stack.parag.llm_client import SyntheticClient, _load_env_file
from search_stack.parag.sac_builder import (
    Chunk,
    _LANG_LABELS,
    _BATCH_SYSTEM,
    _build_user_batch,
    _parse_batch_response,
    LLM_MAX_TOKENS_PER_BATCH,
)

_load_env_file()


def load_sample_batches(parag_db: Path, n_decisions: int) -> list[list[Chunk]]:
    """Return lists of chunks (batches) from N random decisions whose
    chunks were originally flagged summary_source='llm' — so we know
    they deserve a summary and have a Kimi reference stored."""
    conn = sqlite3.connect(str(parag_db))
    conn.row_factory = sqlite3.Row
    decisions = conn.execute("""
        SELECT decision_id FROM chunks
        WHERE summary_source='llm'
        GROUP BY decision_id HAVING COUNT(*) >= 3
        ORDER BY RANDOM() LIMIT ?
    """, (n_decisions,)).fetchall()

    batches: list[list[Chunk]] = []
    for d in decisions:
        rows = conn.execute("""
            SELECT id, decision_id, court, language, considerant_number,
                   depth, span_start, span_end, raw_length, cleaned,
                   summary, summary_source
            FROM chunks
            WHERE decision_id=? AND summary_source='llm'
            ORDER BY span_start
        """, (d["decision_id"],)).fetchall()
        chunks = [
            Chunk(
                decision_id=r["decision_id"],
                court=r["court"],
                language=r["language"],
                considerant_number=r["considerant_number"],
                depth=r["depth"],
                span_start=r["span_start"],
                span_end=r["span_end"],
                raw_length=r["raw_length"],
                cleaned=r["cleaned"],
                reason_skipped=None,
                summary=r["summary"],
                summary_source=r["summary_source"],
            )
            for r in rows
        ][:10]  # cap batch at 10
        batches.append(chunks)
    return batches


def call_openrouter(model: str, system: str, user: str) -> dict:
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY missing")
    url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    url += "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": LLM_MAX_TOKENS_PER_BATCH,
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/dacasine/caselaw-repo-1",
            "X-Title": "PA-RAG SAC benchmark",
        },
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=180) as resp:
        raw = resp.read()
    dt = time.monotonic() - t0
    data = json.loads(raw.decode("utf-8"))
    msg = data.get("choices", [{}])[0].get("message", {})
    return {
        "latency": dt,
        "content": (msg.get("content") or "").strip(),
        "prompt_tokens": data.get("usage", {}).get("prompt_tokens", 0),
        "completion_tokens": data.get("usage", {}).get("completion_tokens", 0),
    }


def call_synthetic(model: str, system: str, user: str) -> dict:
    client = SyntheticClient(model=model, rate_limit_per_minute=30, quota_aware=False)
    resp = client.chat(system=system, user=user,
                       max_tokens=LLM_MAX_TOKENS_PER_BATCH, temperature=0.2)
    return {
        "latency": resp.latency_s,
        "content": resp.content,
        "prompt_tokens": resp.usage.get("prompt_tokens", 0),
        "completion_tokens": resp.usage.get("completion_tokens", 0),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--include-synthetic", action="store_true")
    ap.add_argument("--n-decisions", type=int, default=3)
    args = ap.parse_args()

    parag_db = Path.home() / ".swiss-caselaw" / "parag_chunks.db"
    batches = load_sample_batches(parag_db, args.n_decisions)
    if not batches:
        print("No decisions found with llm-summarised chunks")
        return

    model_list = list(args.models)
    if args.include_synthetic:
        model_list.insert(0, "SYNTHETIC:hf:moonshotai/Kimi-K2-Instruct-0905")

    # Flatten prompts: one batch per decision
    prompts = []
    for chunks in batches:
        lang_label = _LANG_LABELS.get(chunks[0].language, "français")
        sys_p = _BATCH_SYSTEM.replace("{lang}", lang_label)
        user_p = _build_user_batch(
            decision_id=chunks[0].decision_id,
            court=chunks[0].court,
            regeste="",
            chunks=chunks,
        )
        prompts.append((chunks, sys_p, user_p))

    total_chunks = sum(len(b) for b in batches)
    print(f"\n{args.n_decisions} decisions, {total_chunks} chunks total\n")

    for model_id in model_list:
        print(f"\n{'═'*72}")
        print(f"MODEL: {model_id}")
        print(f"{'═'*72}")
        totals = {"latency": 0.0, "prompt": 0, "completion": 0, "parsed": 0}
        for idx, (chunks, sys_p, user_p) in enumerate(prompts, 1):
            try:
                if model_id.startswith("SYNTHETIC:"):
                    resp = call_synthetic(model_id.split(":", 1)[1], sys_p, user_p)
                else:
                    resp = call_openrouter(model_id, sys_p, user_p)
            except urllib.error.HTTPError as e:
                print(f"  batch {idx}: HTTP {e.code}: {e.read()[:200]}")
                continue
            except Exception as e:
                print(f"  batch {idx}: ERROR: {e}")
                continue

            parsed = _parse_batch_response(resp["content"], len(chunks))
            totals["latency"] += resp["latency"]
            totals["prompt"] += resp["prompt_tokens"]
            totals["completion"] += resp["completion_tokens"]
            totals["parsed"] += len(parsed)

            print(f"\n  --- batch {idx}: {chunks[0].decision_id} ({len(chunks)} chunks) ---")
            print(f"  {resp['latency']:.1f}s  in={resp['prompt_tokens']}  "
                  f"out={resp['completion_tokens']}  "
                  f"parsed={len(parsed)}/{len(chunks)}")
            # Show first 2 summaries vs Kimi reference
            for i in range(1, min(3, len(chunks) + 1)):
                cand = parsed.get(i, "(missing)")
                ref = chunks[i-1].summary or "(none)"
                print(f"    [C{i}] cand: {cand[:150]}")
                print(f"         kimi: {ref[:150]}")

        n_batches = len(prompts)
        print(f"\n  SUMMARY:")
        print(f"    avg_lat={totals['latency']/n_batches:.1f}s  "
              f"avg_in={totals['prompt']/n_batches:.0f}  "
              f"avg_out={totals['completion']/n_batches:.0f}  "
              f"parsed_total={totals['parsed']}/{total_chunks}")


if __name__ == "__main__":
    main()
