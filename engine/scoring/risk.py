"""Risk scoring engine (M6).

Additive signal model (0-100): every signal the engine can actually
observe contributes points with a stated reason. Signals are derived
from the traced path's hop classifications and terminal — never from
data we have not ingested.

Explicitly NOT scored here (future work, needs dataset ingestion):
OFAC SDN / ransomware / scam-list proximity. Those arrive with the M7
public-dataset test suite; scoring proximity to lists we have not
loaded would be fabrication.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RiskSignal:
    name: str
    points: int
    reason: str


@dataclass(frozen=True)
class RiskScore:
    total: int                 # 0..100, capped
    level: str                 # "low" | "medium" | "high" | "critical"
    signals: tuple[RiskSignal, ...] = ()


def _level(total: int) -> str:
    if total >= 75:
        return "critical"
    if total >= 50:
        return "high"
    if total >= 25:
        return "medium"
    return "low"


def score_risk(visited: list,
               terminal_reason: str | None = None,
               terminal_vasp_registered: bool | None = None,
               sanctions_hits: tuple[str, ...] = ()) -> RiskScore:
    """Score risk for one traced path.

    visited: TraversalResult.visited.
    terminal_reason: Terminal.reason string, if terminated.
    terminal_vasp_registered: FIU-IND status of the attributed terminal
        VASP, when the terminal was resolved to a directory entry.
    sanctions_hits: addresses on the path that hit a sanctions list
        (OFAC SDN via engine/intel). A direct hit is severe.
    """
    signals: list[RiskSignal] = []
    kinds = [getattr(n, "via_kind", "") for n in visited]

    if sanctions_hits:
        signals.append(RiskSignal(
            "sanctions-list-hit", 50,
            f"{len(sanctions_hits)} address(es) on the traced path appear "
            "on the OFAC SDN list: "
            + ", ".join(a[:12] + "…" for a in sanctions_hits[:3])))
    if "mixer-deposit" in kinds:
        signals.append(RiskSignal(
            "mixer-deposit", 40,
            "funds entered a mixer anonymity set — strongest single "
            "illicit-finance indicator the engine observes"))
    if "swap-service" in kinds or (terminal_reason or "") in (
            "swap-service", "unhandled-hop:swap-service"):
        signals.append(RiskSignal(
            "swap-service", 25,
            "non-KYC swap service used: custodial swap with no on-chain "
            "linkage proof, a classic laundering hop"))
    if "bridge-lock" in kinds:
        signals.append(RiskSignal(
            "cross-chain-bridge", 15,
            "value moved cross-chain: breaks single-ledger visibility, "
            "correlation (not proof) links the far side"))
    peel_run = 0
    longest_peel_run = 0
    for k in kinds:
        peel_run = peel_run + 1 if k == "peel" else 0
        longest_peel_run = max(longest_peel_run, peel_run)
    if longest_peel_run >= 5:
        signals.append(RiskSignal(
            "layering-like-peel-chain", 10,
            f"{longest_peel_run} consecutive peel hops: layering-style "
            "obfuscation pattern"))
    if terminal_vasp_registered is False:
        signals.append(RiskSignal(
            "unregistered-terminal-vasp", 10,
            "trail terminates at a VASP with no FIU-IND registration: "
            "weaker legal reach, higher flight risk"))

    total = min(100, sum(s.points for s in signals))
    return RiskScore(total=total, level=_level(total),
                     signals=tuple(signals))
