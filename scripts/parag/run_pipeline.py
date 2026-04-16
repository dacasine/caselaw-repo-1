"""Full PA-RAG pipeline orchestrator — chains SAC → Embed → Phase 5 per scope.

Usage:
    .venv/bin/python scripts/parag/run_pipeline.py \\
        --scopes bger bvger bstger \\
        --sac-workers 16 --phase5-workers 24

Each scope runs SAC first (creates chunks + summaries), then encodes
the new chunks to vectors, then runs Phase 5 (per-decision enrichment
+ per-chunk citation resolution). Each stage is idempotent — reruns
skip already-processed decisions.

Failures are logged but don't stop the cascade: if SAC partially fails
for one scope, the downstream stages skip those decisions via
idempotence checks.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_step(name: str, cmd: list[str], log_path: Path) -> int:
    """Run a subcommand, streaming output to a log file. Returns exit code."""
    print(f"[{ts()}] ▶ {name}  log={log_path}")
    print(f"       cmd: {' '.join(cmd[:6])}{' …' if len(cmd) > 6 else ''}")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as fh:
        result = subprocess.run(
            cmd, stdout=fh, stderr=subprocess.STDOUT,
            cwd=REPO_ROOT, env={
                **dict(__import__("os").environ),
                "PYTHONPATH": str(REPO_ROOT),
            },
        )
    status = "✓ OK" if result.returncode == 0 else f"✗ exit={result.returncode}"
    print(f"[{ts()}] {status}  {name}")
    return result.returncode


def pipeline_for_scope(
    scope: str,
    *,
    sac_workers: int,
    sac_rate: int,
    sac_model: str,
    phase5_workers: int,
    phase5_rate: int,
    phase5_model: str,
    embed_batch: int,
    light_phase5: bool,
    limit: int | None,
    log_dir: Path,
) -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    py = str(REPO_ROOT / ".venv" / "bin" / "python")

    print(f"\n{'═'*72}")
    print(f"  SCOPE : {scope}")
    print(f"{'═'*72}")

    # 1. SAC — creates chunks + summaries
    cmd = [
        py, "scripts/parag/run_sac.py",
        "--court", scope,
        "--workers", str(sac_workers), "--rate", str(sac_rate),
        "--provider", "openrouter", "--model", sac_model,
    ]
    if limit:
        cmd += ["--limit", str(limit)]
    run_step(f"SAC {scope}", cmd, log_dir / f"pipeline_sac_{scope}_{stamp}.log")

    # 2. Embedder — encode new chunks
    cmd = [
        py, "scripts/parag/run_embed.py",
        "--batch", str(embed_batch), "--device", "cpu",
        "--where", f"c.court = '{scope}'",
    ]
    run_step(f"Embed {scope}", cmd, log_dir / f"pipeline_embed_{scope}_{stamp}.log")

    # 3. Phase 5 — per-decision enrichment + per-chunk citations
    cmd = [
        py, "scripts/parag/run_phase5.py",
        "--court", scope,
        "--workers", str(phase5_workers), "--rate", str(phase5_rate),
        "--model", phase5_model,
    ]
    if light_phase5:
        cmd.append("--light")
    if limit:
        cmd += ["--limit", str(limit)]
    run_step(f"Phase5 {scope}", cmd, log_dir / f"pipeline_phase5_{scope}_{stamp}.log")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scopes", nargs="+", required=True,
                    help="Court codes to process in order (e.g. bger bvger bstger)")
    ap.add_argument("--sac-workers", type=int, default=16)
    ap.add_argument("--sac-rate", type=int, default=300)
    ap.add_argument("--sac-model", default="google/gemini-2.0-flash-001")
    ap.add_argument("--phase5-workers", type=int, default=24)
    ap.add_argument("--phase5-rate", type=int, default=420)
    ap.add_argument("--phase5-model", default="google/gemini-2.0-flash-001")
    ap.add_argument("--embed-batch", type=int, default=2)
    ap.add_argument("--light-phase5", action="store_true",
                    help="Use SYSTEM_PROMPT_LIGHT for Phase 5 (default: FULL)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap decisions per scope (testing)")
    ap.add_argument("--log-dir", type=Path, default=Path("logs"))
    args = ap.parse_args()

    t_start = time.monotonic()
    for scope in args.scopes:
        t_scope = time.monotonic()
        pipeline_for_scope(
            scope,
            sac_workers=args.sac_workers, sac_rate=args.sac_rate,
            sac_model=args.sac_model,
            phase5_workers=args.phase5_workers, phase5_rate=args.phase5_rate,
            phase5_model=args.phase5_model,
            embed_batch=args.embed_batch,
            light_phase5=args.light_phase5,
            limit=args.limit,
            log_dir=args.log_dir,
        )
        dt = time.monotonic() - t_scope
        print(f"[{ts()}] scope {scope} complete in {dt/60:.1f} min")

    total_dt = time.monotonic() - t_start
    print(f"\n[{ts()}] pipeline done in {total_dt/60:.1f} min across "
          f"{len(args.scopes)} scope(s)")


if __name__ == "__main__":
    main()
