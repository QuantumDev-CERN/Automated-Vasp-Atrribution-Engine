"""Async trace pipeline (M7).

One traced wallet, end to end, as a pure async function so the arq
worker, the API, and tests all run the same code:

  adapters -> graph expansion -> classify -> traverse ->
  confidence + risk -> VASP attribution -> report + certificate

Bounded expansion (BFS over addresses, capped) keeps indexer load and
cost predictable — the master reference's "don't re-resolve" caching
note is future work, not silently skipped: repeated runs re-fetch.
"""
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

from ..adapters.base import Chain, ChainAdapter
from ..graph.builder import TxGraph, expand_address
from ..graph.store import GraphStore
from ..intel.sanctions import SanctionsList
from ..intel.mixer_correlation import (
    CAVEAT as MIXER_CORRELATION_CAVEAT,
    MixerWithdrawalCandidate,
    correlate_mixer_withdrawals,
)
from ..intel.otc import OTC_TERMINUS_TAG, detect_otc_termini, registry_contains
from ..report import (
    InvestigationReport, ReportInput, build_report,
)
from ..scoring import (
    AttributionScore, RiskScore, score_attribution, score_risk,
)
from ..traversal.engine import (
    Terminal, TraversalConfig, TraversalResult, VisitedNode, traverse,
)
from ..vasp import (
    CaseDetails, LegalInstrument, RouteRecommendation, VaspRecord,
    find_vasp, recommend_and_draft,
)

# terminal preference when several exist: deepest hop wins, ties broken
# by how actionable the terminal is for an investigator
_TERMINAL_PRIORITY = [
    "sweep-consolidation",   # likely VASP hot wallet: most actionable
    "mixer-deposit",
    "coinjoin",            # anonymity set: like mixer-deposit, probabilistic
    "bridge-lock",
    "swap-service",        # named custodial service + recorded deposit
    "otc-hawala-terminus",  # recognized terminus, not a failed trace
    "dead-end",
]


def _terminal_rank(reason: str) -> int:
    for i, prefix in enumerate(_TERMINAL_PRIORITY):
        if reason.startswith(prefix):
            return i
    return len(_TERMINAL_PRIORITY)


@dataclass
class PipelineDeps:
    """Injectable seams: tests pass fakes, the worker passes real ones."""
    adapter_factory: Callable[[str], ChainAdapter]
    sanctions: Optional[SanctionsList] = None
    # terminal address -> VASP directory label (address->VASP clustering
    # is future work; default resolves nothing rather than guessing)
    vasp_resolver: Callable[[str], Optional[str]] = (
        lambda addr: None)  # noqa: E731
    max_expand_hops: int = 2
    max_expand_addresses: int = 25
    traversal_config: TraversalConfig = field(
        default_factory=TraversalConfig)
    # M8: persist the traced subgraph; None = skip persistence.
    graph_store: Optional[GraphStore] = None
    case_id: Optional[str] = None
    # M13: versioned confidence calibration; None = identity (no model yet).
    calibration: Optional["CalibrationModelRec"] = None
    # M21: destination-chain continuation depth. The primary trace runs at
    # 0; each bridge-lock continuation runs at depth+1 and never continues
    # further (max 1 level: no ping-pong between chains).
    cross_chain_depth: int = 0
    max_cross_chain_depth: int = 1


@dataclass(frozen=True)
class CrossChainContinuation:
    """M21: a follow-up trace on the destination chain of a bridge lock
    whose destination was explicitly parsed from calldata."""
    bridge: str
    src_chain: str
    src_tx_hash: str
    dest_chain: str
    dest_address: str
    terminal_reason: Optional[str]
    terminal_address: Optional[str]
    risk_total: int


@dataclass(frozen=True)
class TraceResult:
    address: str
    chain: str
    path: tuple[VisitedNode, ...]       # subject -> primary terminal
    terminal_reason: Optional[str]
    terminal_address: Optional[str]
    attribution: AttributionScore
    risk: RiskScore
    sanctions_hits: tuple[str, ...]
    terminal_vasp: Optional[VaspRecord]
    route: Optional[RouteRecommendation]
    drafted_request: str
    report: InvestigationReport
    cross_chain: tuple = ()  # M21: CrossChainContinuation records
    mixer_correlation: tuple = ()  # M22: MixerWithdrawalCandidate records


def make_adapter(chain: str) -> ChainAdapter:
    """Real adapter factory from settings (worker path)."""
    from api.core.config import settings
    from ..adapters.bitcoin import BitcoinAdapter
    from ..adapters.covalent import CovalentAdapter
    from ..adapters.evm import EvmAdapter
    from ..adapters.solana import SolanaAdapter
    from ..adapters.tron import TronAdapter

    c = Chain(chain)
    if c == Chain.ETHEREUM:
        return EvmAdapter(chain=c, api_key=settings.etherscan_api_key)
    if c in (Chain.BSC, Chain.POLYGON):
        return CovalentAdapter(chain=c, api_key=settings.covalent_api_key)
    if c == Chain.TRON:
        return TronAdapter(api_key=settings.trongrid_api_key)
    if c == Chain.BITCOIN:
        return BitcoinAdapter()
    if c == Chain.SOLANA:
        return SolanaAdapter()
    raise ValueError(f"unsupported chain: {chain}")


async def _expand(graph: TxGraph, adapter: ChainAdapter, start: str,
                  deps: PipelineDeps) -> None:
    seen = {start}
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    while queue and len(seen) < deps.max_expand_addresses:
        address, depth = queue.popleft()
        if depth > deps.max_expand_hops:
            continue
        try:
            await expand_address(graph, adapter, address, limit=25)
        except Exception:
            continue  # one bad address must not kill the whole trace
        if depth == deps.max_expand_hops:
            continue
        if address not in graph.g:
            continue
        for nbr in graph.g.successors(address):
            if nbr not in seen and len(seen) < deps.max_expand_addresses:
                seen.add(nbr)
                queue.append((nbr, depth + 1))


def _path_to(result: TraversalResult, terminal_addr: str) -> list[VisitedNode]:
    if terminal_addr == result.start:
        # start address not in graph: traverse() recorded a dead-end
        # terminal without visiting anything
        return [VisitedNode(address=result.start, hop=0)]
    by_addr = {n.address: n for n in result.visited}
    path = [by_addr[terminal_addr]]
    while path[-1].address != result.start:
        parent, _tx = result.came_from[path[-1].address]
        path.append(by_addr[parent])
    path.reverse()
    return path


def _pick_terminal(result: TraversalResult) -> Optional[str]:
    if not result.terminals:
        return None
    by_addr = {n.address: n for n in result.visited}
    ranked = sorted(
        result.terminals,
        key=lambda t: (-by_addr.get(t.address, VisitedNode(t.address, 0)).hop,
                       _terminal_rank(t.reason)))
    return ranked[0].address


async def _known_otc_terminus(deps: PipelineDeps, address: str,
                            chain: str) -> bool:
    """True when a prior case already confirmed this address as an
    OTC/hawala terminus — the persistent registry short-circuit."""
    if deps.graph_store is None or not deps.case_id:
        return False
    try:
        entry = await registry_contains(deps.graph_store, address, chain)
    except Exception:
        return False  # registry unreadable: trace normally
    return entry is not None


async def _relabel_otc_termini(result: TraversalResult,
                              adapter: ChainAdapter, chain: str) -> None:
    """M19: dead-end terminals whose on-chain history shows the
    collection pattern (many disparate depositors, no onward movement)
    are relabeled as OTC/hawala termini — a recognized terminus, not a
    failed trace. Only dead-ends are candidates: any other terminal
    already has a better explanation."""
    candidates = [t for t in result.terminals if t.reason == "dead-end"]
    if not candidates:
        return
    try:
        hits = await detect_otc_termini(
            adapter, chain, [t.address for t in candidates])
    except Exception:
        return  # heuristic pass must never fail a trace
    for t in candidates:
        assessment = hits.get(t.address)
        if assessment is not None:
            t.reason = OTC_TERMINUS_TAG
            print(f"[otc] {t.address}: {assessment.detail}")


async def run_trace_pipeline(address: str, chain: str, case: CaseDetails,
                             deps: PipelineDeps) -> TraceResult:
    adapter = deps.adapter_factory(chain)
    graph = TxGraph()
    if await _known_otc_terminus(deps, address, chain):
        # M19: repeat of a confirmed terminus — skip expansion entirely.
        # The lone node keeps the graph-store block below working so the
        # cross-case brief still links the prior case(s).
        print(f"[otc] {address}: known OTC/hawala terminus — short-circuit")
        graph.g.add_node(address, chains={chain}, first_seen=None,
                         labels=set())
        result = TraversalResult(
            start=address,
            visited=[VisitedNode(address=address, hop=0)],
            terminals=[Terminal(address=address, reason=OTC_TERMINUS_TAG)],
        )
    else:
        await _expand(graph, adapter, address, deps)
        result = traverse(graph, address, deps.traversal_config)
        await _relabel_otc_termini(result, adapter, chain)

    terminal_address = _pick_terminal(result)
    terminal_reason = next(
        (t.reason for t in result.terminals
         if t.address == terminal_address), None)
    path = (_path_to(result, terminal_address)
            if terminal_address else [VisitedNode(address=address, hop=0)])

    sanctions_hits: list[str] = []
    if deps.sanctions is not None:
        for node in path:
            if deps.sanctions.lookup(node.address):
                sanctions_hits.append(node.address)

    # M21: explicit bridge destinations (parsed from lock calldata).
    explicit_locks = [b for b in result.bridge_deposits
                      if b.direction == "lock" and b.dest_address]
    first_label = None
    if explicit_locks:
        b0 = explicit_locks[0]
        first_label = (f"{b0.dest_chain}:{b0.dest_address[:12]}…"
                       if b0.dest_chain else b0.dest_address[:12] + "…")

    attribution = score_attribution(
        path, terminal_reason=terminal_reason,
        bridge_explicit_destination=bool(explicit_locks),
        bridge_destination_label=first_label,
        calibration=deps.calibration)
    risk = score_risk(path, terminal_reason=terminal_reason,
                      sanctions_hits=tuple(sanctions_hits))

    # M21: destination-chain continuation. For each lock with an
    # explicitly parsed, adapter-supported destination, run one bounded
    # follow-up trace on the destination chain. One level only, loop
    # guarded, and a failed continuation never fails the primary trace.
    cross_chain: list[CrossChainContinuation] = []
    if deps.cross_chain_depth < deps.max_cross_chain_depth:
        seen: set[tuple[str, str]] = {(chain, address.lower())}
        for b in explicit_locks:
            if not b.dest_chain:
                continue  # parsed, but no adapter for the destination
            pair = (b.dest_chain, b.dest_address.lower())
            if pair in seen:
                continue
            seen.add(pair)
            try:
                sub = await run_trace_pipeline(
                    b.dest_address, b.dest_chain, case,
                    replace(deps,
                            cross_chain_depth=deps.cross_chain_depth + 1))
            except Exception as exc:  # noqa: BLE001
                print(f"[bridge] continuation {b.dest_chain}:"
                      f"{b.dest_address[:12]}… failed: {exc}")
                continue
            cross_chain.append(CrossChainContinuation(
                bridge=b.bridge, src_chain=chain, src_tx_hash=b.tx_hash,
                dest_chain=b.dest_chain, dest_address=b.dest_address,
                terminal_reason=sub.terminal_reason,
                terminal_address=sub.terminal_address,
                risk_total=sub.risk.total))
            print(f"[bridge] continuation {b.dest_chain}:"
                  f"{b.dest_address[:12]}… -> {sub.terminal_reason}")

    # M22: mixer correlation — probabilistic withdrawal candidates for a
    # mixer-deposit terminal. Best-effort, bounded, and a failure never
    # fails the primary trace. Candidates are leads, not attribution.
    mixer_correlation: list[MixerWithdrawalCandidate] = []
    if terminal_reason == "mixer-deposit":
        for md in result.mixer_deposits:
            if terminal_address and md.pool.lower() != terminal_address.lower():
                continue
            try:
                mixer_correlation.extend(await correlate_mixer_withdrawals(
                    md, graph, deps.adapter_factory(md.chain)))
            except Exception as exc:  # noqa: BLE001
                print(f"[mixer] correlation for {md.pool[:12]}… "
                      f"failed: {exc}")
            print(f"[mixer] {len(mixer_correlation)} withdrawal candidate(s) "
                  f"for {md.pool[:12]}…")

    terminal_vasp = route = None
    drafted_request = ""
    if terminal_address:
        label = deps.vasp_resolver(terminal_address)
        if label:
            vasp = find_vasp(label)
            if vasp is not None:
                out = recommend_and_draft(label, case)
                if out:
                    route, drafted_request = out
                    terminal_vasp = vasp

    # M8: materialize the traced subgraph + tag terminal addresses with
    # their traversal classification (persistent vocabulary for M9).
    # Done BEFORE the report so M9's cross-case brief can be certified
    # inside it.
    cross_case_brief = ""
    if deps.graph_store is not None and deps.case_id:
        from ..intel import find_case_links, syndicate_summary

        stats = await deps.graph_store.save_case_subgraph(
            deps.case_id, graph,
            meta={"subject": address, "chain": chain,
                  "terminal": terminal_address,
                  "terminal_reason": terminal_reason})
        for t in result.terminals:
            await deps.graph_store.tag_address(
                t.address, chain, t.reason,
                source=f"traversal:{deps.case_id}", case_id=deps.case_id)
        links = await find_case_links(deps.case_id, deps.graph_store)
        cross_case_brief = syndicate_summary(links)
        print(f"[graph] case {deps.case_id}: persisted {stats}")

    report = build_report(ReportInput(
        case=case, subject_wallet=address, chain=chain,
        attribution=attribution, risk=risk, terminal_vasp=terminal_vasp,
        route=route, drafted_request=drafted_request,
        bridge_deposits=tuple(result.bridge_deposits),
        swap_deposits=tuple(result.swap_deposits),
        cross_chain=tuple(cross_chain),
        mixer_correlation=tuple(mixer_correlation),
        cross_case=cross_case_brief,
        calibration_version=attribution.calibration_version or ""))

    return TraceResult(
        address=address, chain=chain, path=tuple(path),
        terminal_reason=terminal_reason, terminal_address=terminal_address,
        attribution=attribution, risk=risk,
        sanctions_hits=tuple(sanctions_hits),
        terminal_vasp=terminal_vasp, route=route,
        drafted_request=drafted_request, report=report,
        cross_chain=tuple(cross_chain),
        mixer_correlation=tuple(mixer_correlation))
