"""M9: cross-case knowledge graph / syndicate correlation.

M8 persisted per-case subgraphs and a cross-case address index. M9 runs
the queries that turn that index into intelligence:

  * find_case_links — other cases sharing wallets with this case,
    ranked by overlap, with the shared addresses and their tags as
    the explanation ("linked via mixer-deposit 0xabc…").
  * shared_infrastructure — every address carrying a tag (e.g. all
    "mixer-deposit" addresses), grouped by the cases they appear in:
    common-infrastructure pivoting.
  * syndicate_summary — one human-readable brief for the report.

All queries go through the GraphStore ABC, so they work identically on
Neo4j and the memory stand-in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..graph.store import GraphStore


@dataclass
class LinkedCase:
    case_id: str
    shared_addresses: list[str] = field(default_factory=list)
    # address -> [tags] for the shared addresses, when tagged
    shared_tags: dict[str, list[str]] = field(default_factory=dict)

    @property
    def overlap(self) -> int:
        return len(self.shared_addresses)


@dataclass
class CaseLinks:
    case_id: str
    links: list[LinkedCase] = field(default_factory=list)

    @property
    def linked_case_ids(self) -> list[str]:
        return [lc.case_id for lc in self.links]


async def find_case_links(
    case_id: str,
    store: GraphStore,
    *,
    min_overlap: int = 1,
) -> CaseLinks:
    """Other cases sharing at least min_overlap addresses with case_id.

    Ranked by overlap descending. Shared addresses carry their
    provenance tags so the investigator sees *why* two cases link.

    M41: was 1 + N + M sequential store round trips (one per address);
    now 3 total via the batched primitives — the per-address loop
    froze the API on remote Neo4j.
    """
    graph = await store.load_case_subgraph(case_id)
    if graph is None:
        return CaseLinks(case_id=case_id)

    # addr -> aids (f"{chain}:{address}"), preserving the original
    # multi-chain expansion.
    addr_aids: dict[str, list[str]] = {}
    for addr in graph.addresses():
        chains = graph.g.nodes[addr].get("chains") or {"unknown"}
        addr_aids[addr] = [f"{chain}:{addr}" for chain in chains]
    cases_map = await store.cases_for_addresses(
        [aid for aids in addr_aids.values() for aid in aids]
    )

    per_case: dict[str, LinkedCase] = {}
    for addr, aids in addr_aids.items():
        for aid in aids:
            for other in cases_map.get(aid, []):
                if other == case_id:
                    continue
                lc = per_case.setdefault(
                    other, LinkedCase(case_id=other))
                if addr not in lc.shared_addresses:
                    lc.shared_addresses.append(addr)

    shared_addrs = {
        addr for lc in per_case.values() for addr in lc.shared_addresses
    }
    tags_map = await store.tags_for_addresses(
        [aid for addr in shared_addrs for aid in addr_aids[addr]]
    )

    result = CaseLinks(case_id=case_id)
    for lc in per_case.values():
        if lc.overlap < min_overlap:
            continue
        lc.shared_addresses.sort()
        # attach tags for the shared addresses (explanation, not just ids)
        for addr in lc.shared_addresses:
            tags: list[str] = []
            for aid in addr_aids[addr]:
                tags.extend(
                    t["tag"] for t in tags_map.get(aid, []))
            if tags:
                lc.shared_tags[addr] = sorted(set(tags))
        result.links.append(lc)
    result.links.sort(key=lambda lc: (-lc.overlap, lc.case_id))
    return result


async def shared_infrastructure(
    tag: str,
    store: GraphStore,
) -> dict[str, list[str]]:
    """Pivot on common infrastructure: every address carrying `tag`,
    grouped by the cases it appears in.

    Returns {address: [case_ids]} — e.g. tag="mixer-deposit" shows which
    cases funneled through the same mixer deposit wallets.

    M41: was 1 + N sequential round trips; now 2 via cases_for_addresses.
    """
    out: dict[str, list[str]] = {}
    entries = await store.addresses_with_tag(tag)
    aids = [f"{e['chain']}:{e['address']}" for e in entries]
    cases_map = await store.cases_for_addresses(aids)
    for entry, aid in zip(entries, aids):
        out[entry["address"]] = sorted(cases_map.get(aid, []))
    return dict(sorted(out.items()))


def syndicate_summary(links: CaseLinks) -> str:
    """One-paragraph brief for the investigation report."""
    if not links.links:
        return (f"Case {links.case_id}: no cross-case links — none of its "
                f"addresses appear in any other persisted case.")
    lines = [f"Case {links.case_id} links to {len(links.links)} other "
             f"case(s):"]
    for lc in links.links:
        via = []
        for addr in lc.shared_addresses[:5]:
            tags = lc.shared_tags.get(addr)
            label = f"{addr[:14]}…" + (f" [{', '.join(tags)}]" if tags
                                       else "")
            via.append(label)
        more = (f" +{lc.overlap - 5} more"
                if lc.overlap > 5 else "")
        lines.append(f"  - {lc.case_id}: {lc.overlap} shared "
                     f"address(es): {'; '.join(via)}{more}")
    return "\n".join(lines)
