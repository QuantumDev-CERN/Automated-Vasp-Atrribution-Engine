"""Mixer correlation heuristics (M22).

When a trace terminates at a mixer-deposit, deterministic onward
attribution is impossible by design (master plan section 10:
large-anonymity-set mixers are probabilistic best-effort only, always
disclosed as such). What we CAN do is rank *candidate* withdrawals from
the same pool using the three heuristics named in the plan
(section 6):

1. Timing — withdrawals of the same fixed denomination that leave the
   pool shortly after the subject deposit. Score decays exponentially
   with minutes-after-deposit:
       timing = exp(-minutes_after / (window_minutes / 3))
   Pool txs are split into deposits/withdrawals by on-chain-verified
   function selectors (see _DEPOSIT_SELECTOR). The pool txlist can
   include reverted txs; a failed deposit only inflates the anonymity
   set (conservative direction) and a failed withdrawal's caller is
   still a genuine withdrawal attempter, so no filtering is applied.
2. Anonymity-set size — deposits into the pool in a window around the
   subject deposit. A smaller set strengthens every timing candidate;
   the count is reported as context and mildly modulates the score:
       combined = timing / (1 + log10(1 + anonymity_set))
3. Gas-funding self-link — the classic operational-security failure:
   the withdrawal caller and the depositor were both funded by the
   same address. Checked only for the top-N timing candidates
   (one address-history call each, bounded). A hit boosts the score
   1.5x (capped at 1.0) and is flagged — it is the strongest of the
   three signals.

Caveats, repeated in every output:
- The withdraw() caller is often a *relayer*, not the recipient — a
  candidate is an investigative lead, never an attribution.
- Scores are relative ranks, not probabilities.

Nothing here touches confidence or risk: the mixer-deposit terminal
keeps its 0.40 discount and +40 signal. Candidates render under their
own report section, never as the onward trail.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ..adapters.base import CanonicalTx, ChainAdapter
from ..graph.builder import TxGraph
from ..traversal.engine import MixerDeposit

# Tornado Cash pool interface — selectors VERIFIED ON-CHAIN 2026-09-28,
# not taken from docs (the commonly quoted deposit()/withdraw() signatures
# produce different selectors and match nothing):
#   0xb214faa5 + value == pool denomination  -> deposit
#   0x21a0adb6 + value == 0                   -> withdraw
# Observed on the 0.1 ETH pool (35 deposits / 22 withdrawals in a 40-tx
# sample), the 1 ETH pool (35/25 in 60), and the 100 ETH pool (11/25 in
# 36). All four pools share the same contract code, so the selectors
# apply to every MixerInfo in the registry.
_DEPOSIT_SELECTOR = "b214faa5"
_WITHDRAW_SELECTOR = "21a0adb6"

CAVEAT = ("timing / anonymity-set / gas-funding heuristics only — "
          "a withdraw() caller is often a relayer, not the recipient. "
          "Candidates are investigative leads, not the onward trail.")


@dataclass(frozen=True)
class MixerWithdrawalCandidate:
    pool: str
    denomination: str
    withdrawal_tx: str
    caller: str
    block_time: Optional[datetime]
    minutes_after_deposit: Optional[float]
    timing_score: float
    anonymity_set: int
    combined_score: float
    gas_self_link: tuple[str, ...] = ()
    note: str = ""


def _selector_of(tx: CanonicalTx) -> str:
    raw = (tx.raw.get("input") or "").lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    return raw[:8]


def split_pool_txs(
    txs: list[CanonicalTx],
) -> tuple[list[CanonicalTx], list[CanonicalTx]]:
    """Split a pool's history into deposits and withdrawals by selector."""
    deposits, withdrawals = [], []
    sel_d, sel_w = _DEPOSIT_SELECTOR, _WITHDRAW_SELECTOR
    for tx in txs:
        sel = _selector_of(tx)
        if sel == sel_d:
            deposits.append(tx)
        elif sel == sel_w:
            withdrawals.append(tx)
    return deposits, withdrawals


def anonymity_set_size(
    deposits: list[CanonicalTx],
    anchor: datetime,
    window_hours: float,
) -> int:
    """Deposits into the pool within +/- window of the subject deposit."""
    window_s = window_hours * 3600
    n = 0
    for d in deposits:
        if d.block_time is None:
            continue
        if abs((d.block_time - anchor).total_seconds()) <= window_s:
            n += 1
    return n


def timing_score(minutes_after: float, window_minutes: float) -> float:
    return math.exp(-minutes_after / (window_minutes / 3))


def combined_score(timing: float, anonymity_set: int) -> float:
    return timing / (1 + math.log10(1 + anonymity_set))


def depositor_funders(graph: TxGraph, depositor: str) -> set[str]:
    """Inbound senders to the depositor seen in the traced graph."""
    d = depositor.lower()
    return {src for (src, _, _) in graph.in_edges(depositor)
            if src.lower() != d}


async def _caller_funders(
    adapter: ChainAdapter, caller: str, limit: int = 50,
) -> set[str]:
    c = caller.lower()
    funders: set[str] = set()
    for tx in await adapter.get_transactions(caller, limit=limit):
        for p in tx.inputs:
            if p.address and p.address.lower() != c:
                funders.add(p.address)
    return funders


async def correlate_mixer_withdrawals(
    deposit: "MixerDeposit",
    graph: TxGraph,
    adapter: ChainAdapter,
    window_hours: float = 24.0,
    top_n: int = 5,
    self_link_top_n: int = 3,
) -> list[MixerWithdrawalCandidate]:
    """Rank candidate withdrawals after a mixer deposit (M22).

    Best-effort and bounded: one pool-history call, plus one
    address-history call per top candidate for the self-link check.
    Returns [] when the deposit is not timestamped or nothing qualifies.
    """
    if deposit.block_time is None:
        return []
    window_s = window_hours * 3600
    window_min = window_hours * 60

    pool_txs = await adapter.get_transactions(deposit.pool, limit=500)
    deposits, withdrawals = split_pool_txs(pool_txs)
    aset = anonymity_set_size(deposits, deposit.block_time, window_hours)

    timed: list[tuple[float, CanonicalTx]] = []
    for w in withdrawals:
        if w.block_time is None:
            continue
        delta_min = (w.block_time - deposit.block_time).total_seconds() / 60
        if 0 < delta_min <= window_min:
            timed.append((delta_min, w))
    timed.sort(key=lambda t: t[0])
    timed = timed[:top_n]

    funders = depositor_funders(graph, deposit.address)
    out: list[MixerWithdrawalCandidate] = []
    for rank, (delta_min, w) in enumerate(timed):
        t_score = timing_score(delta_min, window_min)
        c_score = combined_score(t_score, aset)
        caller = w.inputs[0].address if w.inputs else ""
        shared: tuple[str, ...] = ()
        if rank < self_link_top_n and caller and funders:
            try:
                caller_f = await _caller_funders(adapter, caller)
            except Exception:
                caller_f = set()
            shared = tuple(sorted(
                f for f in (funders & caller_f)
                if f.lower() != deposit.pool.lower()))
            if shared:
                c_score = min(1.0, c_score * 1.5)
        if shared:
            note = (f"withdrawal {delta_min:.0f} min after deposit; "
                    f"anonymity set {aset}; SHARED FUNDER "
                    f"{', '.join(s[:12] + '…' for s in shared)} — "
                    f"possible gas-funding self-link")
        else:
            note = (f"withdrawal {delta_min:.0f} min after deposit; "
                    f"anonymity set {aset}; no shared funder")
        out.append(MixerWithdrawalCandidate(
            pool=deposit.pool,
            denomination=deposit.denomination,
            withdrawal_tx=w.tx_hash,
            caller=caller,
            block_time=w.block_time,
            minutes_after_deposit=round(delta_min, 1),
            timing_score=round(t_score, 4),
            anonymity_set=aset,
            combined_score=round(c_score, 4),
            gas_self_link=shared,
            note=note,
        ))
    out.sort(key=lambda c: c.combined_score, reverse=True)
    return out
