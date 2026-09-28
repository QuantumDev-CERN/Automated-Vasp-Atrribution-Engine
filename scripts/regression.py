"""Regression gate — run after EVERY milestone to prove nothing broke.

Unit tests always run (fast, offline). Live smoke tests run only with
--live: they need indexer keys in .env and burn API quota, so they are
opt-in.

  uv run python scripts/regression.py           # unit stages (CI-safe)
  uv run python scripts/regression.py --live    # + live smokes (M1-M4, M16)

Exit code is 0 only if every enabled stage passes.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STAGES_UNIT = [
    ("unit: pytest (all milestones)", [sys.executable, "-m", "pytest", "-q"]),
    ("unit: VASP directory + routing smoke M5",
     [sys.executable, "scripts/smoke_m5.py"]),
    ("unit: confidence + risk + report smoke M6",
     [sys.executable, "scripts/smoke_m6.py"]),
    ("unit: async jobs + webhook + sanctions smoke M7",
     [sys.executable, "scripts/smoke_m7.py"]),
    ("unit: M7 integration (SKIPs without docker services)",
     [sys.executable, "scripts/smoke_m7_integration.py"]),
    ("unit: M8 graph store (memory) + Neo4j integration (SKIPs w/o docker)",
     [sys.executable, "scripts/smoke_m8_integration.py"]),
]
STAGES_LIVE = [
    ("live: adapter smokes M1+M2", [sys.executable, "scripts/smoke_adapters.py"]),
    ("live: graph/classifier/traversal smoke M3",
     [sys.executable, "scripts/smoke_m3.py"]),
    ("live: DEX swap + SPL-owner decoding smoke M4",
     [sys.executable, "scripts/smoke_m4.py"]),
    ("live: CREATE2 deposit-proxy detection smoke M16",
     [sys.executable, "scripts/smoke_m16.py"]),
    ("live: CoinJoin guard + co-input clustering smoke M17",
     [sys.executable, "scripts/smoke_m17.py"]),
    ("live: swap-service registry re-verification smoke M18",
     [sys.executable, "scripts/smoke_m18.py"]),
    ("live: OTC/hawala terminus + registry smoke M19",
     [sys.executable, "scripts/smoke_m19.py"]),
    ("live: CoinJoin terminal + mixer path-fix smoke M20",
     [sys.executable, "scripts/smoke_m20.py"]),
    ("live: bridge destination parsing + continuation smoke M21",
     [sys.executable, "scripts/smoke_m21.py"]),
    ("live: mixer correlation heuristics smoke M22",
     [sys.executable, "scripts/smoke_m22.py"]),
    ("live: structuring/smurfing signals smoke M23",
     [sys.executable, "scripts/smoke_m23.py"]),
]


def run_stage(name: str, cmd: list[str]) -> bool:
    print(f"\n=== {name} ===", flush=True)
    try:
        p = subprocess.run(
            cmd, cwd=ROOT, capture_output=True, text=True, timeout=900
        )
        out = (p.stdout + p.stderr).strip()
        print("\n".join(out.splitlines()[-12:]))
        ok = p.returncode == 0
        skipped = ok and "SKIP" in (p.stdout or "")
    except subprocess.TimeoutExpired:
        print("TIMEOUT after 900s")
        ok, skipped = False, False
    status = "FAIL" if not ok else ("PASS (with skips)" if skipped else "PASS")
    print(f"--- {status}: {name} ---", flush=True)
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
