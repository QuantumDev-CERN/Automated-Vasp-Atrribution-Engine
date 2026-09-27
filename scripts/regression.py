"""Regression gate — run after EVERY milestone to prove nothing broke.

Unit tests always run (fast, offline). Live smoke tests run only with
--live: they need indexer keys in .env and burn API quota, so they are
opt-in.

  uv run python scripts/regression.py           # unit stages (CI-safe)
  uv run python scripts/regression.py --live    # + live smokes (M1/M2/M3)

Exit code is 0 only if every enabled stage passes.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STAGES_UNIT = [
    ("unit: pytest (all milestones)", [sys.executable, "-m", "pytest", "-q"]),
]
STAGES_LIVE = [
    ("live: adapter smokes M1+M2", [sys.executable, "scripts/smoke_adapters.py"]),
    ("live: graph/classifier/traversal smoke M3",
     [sys.executable, "scripts/smoke_m3.py"]),
]


def run_stage(name: str, cmd: list[str]) -> bool:
    print(f"\n=== {name} ===", flush=True)
    try:
        p = subprocess.run(
            cmd, cwd=ROOT, capture_output=True, text=True, timeout=900
        )
        tail = (p.stdout + p.stderr).strip().splitlines()[-12:]
        print("\n".join(tail))
        ok = p.returncode == 0
    except subprocess.TimeoutExpired:
        print("TIMEOUT after 900s")
        ok = False
    print(f"--- {'PASS' if ok else 'FAIL'}: {name} ---", flush=True)
    return ok


def main() -> None:
    live = "--live" in sys.argv
    stages = STAGES_UNIT + (STAGES_LIVE if live else [])
    if not live:
        print("unit stages only — pass --live to also run the live smoke tests")
    results = [(name, run_stage(name, cmd)) for name, cmd in stages]
    print("\n=== REGRESSION SUMMARY ===")
    for name, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if all(ok for _, ok in results):
        print("\nREGRESSION OK — nothing broke.")
        raise SystemExit(0)
    print("\nREGRESSION FAILED — see stage output above.")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
