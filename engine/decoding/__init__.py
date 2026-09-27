"""M4/M16 decoding: DEX swaps, bridge/mixer correlation, deposit proxies.

- swaps: detect DEX swaps from adapter data (transfer pairing) or from
  Swap event logs in a tx receipt (exact decode).
- correlation: score candidate withdrawal txs against a known deposit
  (bridge lock or mixer deposit) by temporal proximity + amount match.
- proxies (M16): mark EVM output addresses that look like CREATE2 /
  EIP-1167 exchange deposit proxies (opt-in enrichment for sweep tracing).
"""
