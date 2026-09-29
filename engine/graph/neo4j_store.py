"""M8: Neo4j GraphStore implementation.

Schema:
  (:Address {id, address, chain, first_seen})
      id = f"{chain}:{address}" — unique; one node per chain-address pair
      so the same hex on ETH and BSC stays distinct (shard-by-chain).
  (:Address)-[:SENT {tx_hash, value, asset_kind, asset_symbol,
                     asset_contract, asset_decimals, block_time,
                     block_number}]->(:Address)
      one edge per address->address transfer (TxGraph's MultiDiGraph
      edges, keyed the same way).
  (:Case {id, saved_at, snapshot})-[:INCLUDES]->(:Address)
      snapshot = full-fidelity TxGraph JSON (see store.snapshot_graph);
      the SENT skeleton is what M9 queries, the snapshot is what
      load_case_subgraph() restores exactly.
  (:Address)-[:TAGGED {source, at}]->(:Tag {name})
      persistent provenance labels ("mixer-deposit", "vasp-hot-wallet",
      ...) — the vocabulary M9's syndicate correlation runs on.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .builder import TxGraph
from .store import GraphStore, _utcnow_iso, restore_graph, snapshot_graph


class Neo4jGraphStore(GraphStore):
    def __init__(self, uri: str, user: str, password: str, database: str = "") -> None:
        from neo4j import GraphDatabase

        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._database = database or None  # None = server default database
        self._ensure_constraints()

    def _session(self):
        return self._driver.session(database=self._database)

    def ping(self) -> None:
        with self._session() as s:
            s.run("RETURN 1").single()

    def _ensure_constraints(self) -> None:
        with self._session() as s:
            s.run(
                "CREATE CONSTRAINT IF NOT EXISTS "
                "FOR (a:Address) REQUIRE a.id IS UNIQUE"
            )
            s.run(
                "CREATE CONSTRAINT IF NOT EXISTS "
                "FOR (c:Case) REQUIRE c.id IS UNIQUE"
            )
            s.run(
                "CREATE CONSTRAINT IF NOT EXISTS "
                "FOR (t:Tag) REQUIRE t.name IS UNIQUE"
            )

    # ------------------------------------------------------------------ write

    async def save_case_subgraph(
        self,
        case_id: str,
        graph: TxGraph,
        meta: Optional[dict[str, Any]] = None,
    ) -> dict[str, int]:
        snap = snapshot_graph(graph)
        stats = graph.stats()
        nodes = [
            {
                "id": f"{chain}:{addr}",
                "address": addr,
                "chain": chain,
                "first_seen": (
                    graph.g.nodes[addr].get("first_seen").isoformat()
                    if graph.g.nodes[addr].get("first_seen") else None
                ),
            }
            for addr in graph.addresses()
            for chain in (graph.g.nodes[addr].get("chains") or {"unknown"})
        ]
        edges = [
            {
                "src": f"{a.get('chain', 'unknown')}:{src}",
                "dst": f"{a.get('chain', 'unknown')}:{dst}",
                "tx_hash": a.get("tx_hash"),
                "value": str(a.get("value")),
                "asset_kind": a.get("asset_kind"),
                "asset_symbol": a.get("asset_symbol"),
                "asset_contract": a.get("asset_contract"),
                "asset_decimals": a.get("asset_decimals"),
                "block_time": a.get("block_time"),
                "block_number": a.get("block_number"),
            }
            for src, dst, _key, a in graph.g.edges(keys=True, data=True)
        ]
        with self._session() as s:
            s.run(
                "MERGE (c:Case {id: $cid}) "
                "SET c.saved_at = $at, c.snapshot = $snap, "
                "    c.addresses = $n_addr, c.transfers = $n_tx, "
                "    c.transactions = $n_txs, c.meta = $meta",
                cid=case_id, at=_utcnow_iso(),
                snap=json.dumps(snap), n_addr=stats["addresses"],
                n_tx=stats["transfers"], n_txs=stats["transactions"],
                meta=json.dumps(meta or {}),
            )
            if nodes:
                s.run(
                    "UNWIND $nodes AS n "
                    "MERGE (a:Address {id: n.id}) "
                    "SET a.address = n.address, a.chain = n.chain, "
                    "    a.first_seen = n.first_seen",
                    nodes=nodes,
                )
                s.run(
                    "MATCH (c:Case {id: $cid}) "
                    "UNWIND $nodes AS n MATCH (a:Address {id: n.id}) "
                    "MERGE (c)-[:INCLUDES]->(a)",
                    cid=case_id, nodes=nodes,
                )
            if edges:
                s.run(
                    "UNWIND $edges AS e "
                    "MATCH (s:Address {id: e.src}) "
                    "MATCH (d:Address {id: e.dst}) "
                    "CREATE (s)-[:SENT {tx_hash: e.tx_hash, value: e.value,"
                    " asset_kind: e.asset_kind, asset_symbol: e.asset_symbol,"
                    " asset_contract: e.asset_contract,"
                    " asset_decimals: e.asset_decimals,"
                    " block_time: e.block_time,"
                    " block_number: e.block_number}]->(d)",
                    edges=edges,
                )
        return stats

    async def tag_address(
        self,
        address: str,
        chain: str,
        tag: str,
        *,
        source: str,
        case_id: Optional[str] = None,
    ) -> None:
        with self._session() as s:
            s.run(
                "MERGE (a:Address {id: $aid}) "
                "SET a.address = $addr, a.chain = $chain "
                "MERGE (t:Tag {name: $tag}) "
                "MERGE (a)-[r:TAGGED]->(t) "
                "SET r.source = $source, r.at = $at, r.case_id = $cid",
                aid=f"{chain}:{address}", addr=address, chain=chain,
                tag=tag, source=source, at=_utcnow_iso(),
                cid=case_id or "",
            )

    # ------------------------------------------------------------------ read

    async def load_case_subgraph(self, case_id: str) -> Optional[TxGraph]:
        with self._session() as s:
            rec = s.run(
                "MATCH (c:Case {id: $cid}) RETURN c.snapshot AS snap",
                cid=case_id,
            ).single()
        if rec is None or not rec["snap"]:
            return None
        return restore_graph(json.loads(rec["snap"]))

    async def case_stats(self, case_id: str) -> Optional[dict[str, int]]:
        with self._session() as s:
            rec = s.run(
                "MATCH (c:Case {id: $cid}) "
                "RETURN c.addresses AS a, c.transfers AS t, "
                "       c.transactions AS x",
                cid=case_id,
            ).single()
        if rec is None or rec["a"] is None:
            return None
        return {"addresses": rec["a"], "transfers": rec["t"] or 0,
                "transactions": rec["x"] or 0}

    async def case_meta(self, case_id: str) -> Optional[dict[str, Any]]:
        with self._session() as s:
            rec = s.run(
                "MATCH (c:Case {id: $cid}) RETURN c.meta AS meta",
                cid=case_id,
            ).single()
        if rec is None or not rec["meta"]:
            return None
        return json.loads(rec["meta"])

    async def address_tags(
        self, address: str, chain: str
    ) -> list[dict[str, str]]:
        with self._session() as s:
            rows = s.run(
                "MATCH (a:Address {id: $aid})-[r:TAGGED]->(t:Tag) "
                "RETURN t.name AS tag, r.source AS source, "
                "       r.case_id AS case_id, r.at AS at",
                aid=f"{chain}:{address}",
            )
            return [dict(r) for r in rows]

    async def cases_for_address(
        self, address: str, chain: str
    ) -> list[str]:
        with self._session() as s:
            rows = s.run(
                "MATCH (c:Case)-[:INCLUDES]->(a:Address {id: $aid}) "
                "RETURN DISTINCT c.id AS cid ORDER BY cid",
                aid=f"{chain}:{address}",
            )
            return [r["cid"] for r in rows]

    async def addresses_with_tag(self, tag: str) -> list[dict[str, str]]:
        with self._session() as s:
            rows = s.run(
                "MATCH (a:Address)-[:TAGGED]->(:Tag {name: $tag}) "
                "RETURN DISTINCT a.address AS address, a.chain AS chain "
                "ORDER BY a.chain, a.address",
                tag=tag,
            )
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------ life

    async def close(self) -> None:
        self._driver.close()

    @property
    def backend(self) -> str:
        return "neo4j"
