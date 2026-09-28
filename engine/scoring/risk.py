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
               sanctions_hits: tuple[str, ...] = (),
               structuring_findings: tuple = (),
               threat_hits: tuple = ()) -> RiskScore:
    """Score risk for one traced path.

    visited: TraversalResult.visited.
    terminal_reason: Terminal.reason string, if terminated.
    terminal_vasp_registered: FIU-IND status of the attributed terminal
        VASP, when the terminal was resolved to a directory entry.
    sanctions_hits: addresses on the path that hit a sanctions list
        (OFAC SDN via engine/intel). A direct hit is severe.
    structuring_findings: StructuringFinding records from
        engine/intel/structuring.py (M23). Each becomes its own signal.
    threat_hits: (address, ThreatRecord) pairs from
        engine/intel/threat_feeds.py (M24). One signal per address;
        multiple records for the same address are merged into it.
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
    if "coinjoin" in kinds or (terminal_reason or "") == "coinjoin":
        signals.append(RiskSignal(
            "coinjoin", 40,
            "funds entered a CoinJoin anonymity set (collaborative "
            "mixing) — deliberate obfuscation, no deterministic onward "
            "linkage"))
    if (terminal_reason or "") == "otc-hawala-terminus":
        signals.append(RiskSignal(
            "otc-hawala-terminus", 30,
            "funds settled off-chain at a suspected OTC/hawala collection "
            "wallet — many disparate depositors, no onward on-chain "
            "movement: classic cash-settlement laundering pattern"))
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
    for f in structuring_findings:
        # M23: each finding is its own signal; the reason carries the
        # raw counts so a reviewer can judge the shape at a glance.
        addr = f.address[:12] + "…"
        amt = f"{f.amount_min:g}–{f.amount_max:g} {f.asset_symbol}".strip()
        if f.pattern == "fan-out-burst":
            signals.append(RiskSignal(
                "structuring-fan-out", f.points,
                f"{f.count} similar-sized payouts ({amt}, CV {f.cv:g}) "
                f"from {addr} within {f.window_hours:g}h — smurfing-style "
                "distribution"))
        elif f.pattern == "fan-in-burst":
            signals.append(RiskSignal(
                "structuring-fan-in", f.points,
                f"{f.count} similar-sized deposits ({amt}, CV {f.cv:g}) "
                f"into {addr} within {f.window_hours:g}h — smurfing-style "
                "collection"))
        elif f.pattern == "sub-threshold":
            signals.append(RiskSignal(
                "structuring-sub-threshold", f.points,
                f"{f.count} transfers just below the round "
                f"{f.round_number:g} {f.asset_symbol} mark "
                f"({addr}, {f.window_hours:g}h window) — classic "
                "threshold-evasion signature"))

    # M24: one signal per threat-listed address; merge multiple
    # records (e.g. several ransomware families) into the reason.
    by_address: dict[str, list] = {}
    for addr, rec in threat_hits:
        by_address.setdefault(addr, []).append(rec)
    for addr, recs in by_address.items():
        short = addr[:12] + "…"
        if any(r.category == "ransomware" for r in recs):
            fams = sorted({r.label for r in recs if r.label})
            signals.append(RiskSignal(
                "ransomware-direct-hit", 40,
                f"{short} appears in the Ransomwhere crowdsourced "
                f"ransomware-payment dataset (family: "
                f"{', '.join(fams) or 'unlabeled'}) — crowdsourced "
                "lead, accuracy not independently verified"))
        elif any(r.category == "scam" for r in recs):
            signals.append(RiskSignal(
                "scam-direct-hit", 25,
                f"{short} appears in the ScamSniffer community "
                "scam/phishing/drainer blacklist — community intel, "
                "not an authoritative finding"))

    total = min(100, sum(s.points for s in signals))
    return RiskScore(total=total, level=_level(total),
                     signals=tuple(signals))
