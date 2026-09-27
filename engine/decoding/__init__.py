"""M4 decoding: DEX swaps, bridge/mixer correlation.

- swaps: detect DEX swaps from adapter data (transfer pairing) or from
  Swap event logs in a tx receipt (exact decode).
- correlation: score candidate withdrawal txs against a known deposit
  (bridge lock or mixer deposit) by temporal proximity + amount match.
"""
