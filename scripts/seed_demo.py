"""Bulk evaluation seed (M27, expanded M28).

Populates the console with REAL data through the REAL backend: 66
evaluation cases traced by the actual pipeline (worker.trace_wallet —
the same function the arq worker runs), 18 watchlist subscriptions
with baseline + catch-up check cycles each, 2 demo operator accounts,
and a sample of analyst-review artefacts (alert dispositions, feedback
outcomes). The API then serves everything to the frontend; nothing is
fabricated, no frontend fixtures.

Every case is explicitly labeled EVALUATION — fir_number
EVAL/2026/NNNN, notes carrying the public source + provenance and the
disclaimer "not a real investigation". Threat-feed hits, risk scores,
terminals and filings all come out of the engine.

Case subjects (all on-chain activity verified before seeding):
- EVAL/2026/0001-0008: ScamSniffer ETH phishing/scam addresses
- EVAL/2026/0009-0014: Ransomwhere BTC ransomware-payment addresses
- EVAL/2026/0015-0016: real Tornado Cash 1 ETH pool depositors
- EVAL/2026/0017-0046: 30 more ScamSniffer ETH addresses (deterministic
  sample seed 20260929 of the 2026-09-28 snapshot; activity verified
  on-chain 2026-09-28 via the engine's own EVM adapter)
- EVAL/2026/0047-0066: 20 more Ransomwhere BTC addresses, stratified
  across ransomware families (activity verified via mempool.space
  2026-09-28)

Watch alert catch-up (documented, not hidden): the first tick learns
its baseline from the 5 most recent transactions; the second tick runs
the normal 25-tx window, so the address's own real recent history
surfaces as the watch's initial catch-up alerts. Every alerted tx is a
genuine on-chain movement of the watched address — nothing synthetic.
A few alerts carry simulated analyst dispositions, explicitly labeled.

Idempotent: stable evaluation identifiers. Re-running skips cases that
already have a report and watches that already exist; a case whose
latest trace failed is retried with a fresh job.

Infra: Postgres is REQUIRED (a memory-store seed would be invisible
to the API — the script refuses instead of pretending). Neo4j is
used when reachable; otherwise graph pages stay empty and the script
says so loudly. Indexer API keys come from .env like everything else.

Run from the repository root:
    uv run python scripts/seed_demo.py --yes
    uv run python scripts/seed_demo.py --dry-run   # show the plan only
    SEED_DEMO=1 uv run python scripts/seed_demo.py # env opt-in
    uv run python scripts/seed_demo.py --yes --cases EVAL/2026/0001,EVAL/2026/0009
"""
import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)  # Pydantic's .env path is relative to the CWD
sys.path.insert(0, str(REPO_ROOT))

EVAL = ("EVALUATION SEED — not a real investigation, not a real FIR. "
        "Subject is a public address from a public threat-intel feed, "
        "traced by the engine for demonstration.")

# ---------------------------------------------------------------- cases
# source: exact public provenance for each subject. 38 ScamSniffer ETH
# phishing/scam addresses (snapshot data/threat_feeds/scamsniffer-*.json),
# 26 Ransomwhere BTC ransomware-payment addresses (snapshot
# data/threat_feeds/ransomwhere-*.json), 2 real Tornado Cash 1 ETH pool
# depositors (selector 0xb214faa5 verified empirically 2026-09-28 —
# never seed the pool itself: it traces to a dead end).
CASES: list[dict] = [
    dict(eval_id="EVAL/2026/0001", address="0xbf28eb4e017e472f9f6c11efabcdcb97f5761707",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot 2026-09-28"),
    dict(eval_id="EVAL/2026/0002", address="0x22af344920a302ae1edfc8bfd10a44d395723e46",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot 2026-09-28"),
    dict(eval_id="EVAL/2026/0003", address="0x000000009b0e0800549dba4210a24eabbc93f9ef",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot 2026-09-28"),
    dict(eval_id="EVAL/2026/0004", address="0xe5100c85dc221758d9ceba53cc0e097ccb836ed0",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot 2026-09-28"),
    dict(eval_id="EVAL/2026/0005", address="0x211bda5034fa0f01948f91671d77e814c4ceab59",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot 2026-09-28"),
    dict(eval_id="EVAL/2026/0006", address="0x1d5071048370df50839c8879cdf5144ace4b3b3b",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot 2026-09-28"),
    dict(eval_id="EVAL/2026/0007", address="0xe52331cac0cec1d6b7f0032500fb6aa52c9a3ae6",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot 2026-09-28"),
    dict(eval_id="EVAL/2026/0008", address="0x4f44bd9853737e37668a91963e1b20a0ef52e30c",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot 2026-09-28"),
    dict(eval_id="EVAL/2026/0009", address="17TMc2UkVRSga2yYvuxSD9Q1XyB2EPRjTF",
         chain="bitcoin", label="Ransomwhere: Netwalker/Mailto ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 (crowdsourced)"),
    dict(eval_id="EVAL/2026/0010", address="1DUBrMcH9T13oFSa59jxtFDM5eWTP8v2yc",
         chain="bitcoin", label="Ransomwhere: Ako ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 (crowdsourced)"),
    dict(eval_id="EVAL/2026/0011", address="1FEsU4nL3WBG4YWmzR9BwKtdn9ALqobWJ3",
         chain="bitcoin", label="Ransomwhere: HC6/HC7 ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 (crowdsourced)"),
    dict(eval_id="EVAL/2026/0012", address="1Bq6UcakLLKHQihttwQQjaoSHFci9wGTHu",
         chain="bitcoin", label="Ransomwhere: Locky ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 (crowdsourced)"),
    dict(eval_id="EVAL/2026/0013", address="1BgSZRc3bPD8JfhrcXLSyLqBxaLT8UyTqZ",
         chain="bitcoin", label="Ransomwhere: Locky ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 (crowdsourced)"),
    dict(eval_id="EVAL/2026/0014", address="1GXNfLB4my7F2Xck9Xhy3x6SnVkgMGoZFQ",
         chain="bitcoin", label="Ransomwhere: Locky ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 (crowdsourced)"),
    dict(eval_id="EVAL/2026/0015", address="0xb97ef6609fb8b61611c1bcdb476a01af229cd617",
         chain="ethereum", label="Tornado Cash 1 ETH pool depositor (real deposit tx)",
         source="derived on-chain 2026-09-28: deposit selector 0xb214faa5 into "
                "0x47CE0C6eD5B0Ce3d3A51fdb1C52DC66a7c3c2936"),
    dict(eval_id="EVAL/2026/0016", address="0x9f5b7528dd288f4058672e9f6134e43857a009c3",
         chain="ethereum", label="Tornado Cash 1 ETH pool depositor (real deposit tx)",
         source="derived on-chain 2026-09-28: deposit selector 0xb214faa5 into "
                "0x47CE0C6eD5B0Ce3d3A51fdb1C52DC66a7c3c2936"),
    dict(eval_id="EVAL/2026/0017", address="0xe10da2c1edac5e2061b19b3504a7608142d96f59",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0018", address="0x0000098a312e1244f313f83cac319603a97f4582",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0019", address="0x08a66c51e2d16a44c91592c8d5e62ef94bcbf4aa",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0020", address="0xe0ce8577cbe16e11cac4158ec62b9f8a561c3616",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0021", address="0xf02c21668962abba296040c276987ea7fa4d4cc4",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0022", address="0xc9984286393f8b564473f682f34bde4898049d31",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0023", address="0x8e3e1b2ad7a5b5d7e8d72942a4a239ef6ac6abff",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0024", address="0x71555fd3c87db56738b25f497302fd33f636cf04",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0025", address="0x3253e71aef7b8e181062a4b07c57fa85bf12bdd2",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0026", address="0xc1e3224ecec7e216ca2acfba621e523743168c72",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0027", address="0xf61977431642b6ad4903e7f57a5110aa1f925183",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0028", address="0x59fed4cbd3434256cf0c23e4361035704e6f5579",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0029", address="0x21d81ae7c7ccdd9899dbd898231fbda9c036c7c5",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0030", address="0xc63c55b472fe15b7f580097086753a380e40cdd8",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0031", address="0x36f0ee52adb16837cde15ea4f7c8b38e7e0f6841",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0032", address="0xf2388f790ac052888eee91d8463b257f0b9d2b42",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0033", address="0x915c2aa279a49e9c98917b74d8a42af66eafe8f2",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0034", address="0x85b67c9619da96a412e63a561445ee608c284148",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0035", address="0xc3e6157dfe1bfc2bd93cf74cde85b0ca7ba77aa8",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0036", address="0xa7b1ae65d2f743870fb96950b3bdf0940f2d9b1a",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0037", address="0x66dc1f8dd762c182a9bd9dce47b181c9c414e656",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0038", address="0x60d12d2f360de2bbc404164933005156dbc590fc",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0039", address="0xadf363c6090f911f20c1b59a99928bcb5eed0e5c",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0040", address="0x37a51fd428bc37d5a3be6c43b32446c602c1983b",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0041", address="0xd2613e6e8818967349b8eef41584aaa908a450cb",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0042", address="0xa0db38548d69879e844020742bfd888c28dcd17f",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0043", address="0x9b56aaa0937047a211ad41b9ce5a4c0d731a442e",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0044", address="0x4258ebe8ca35de27d7f60a2512015190b8ad70e7",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0045", address="0xb611e5b4a1c567957d5589cd896b078bab203942",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0046", address="0x5e7e56d8a1c57aa910d59a81b5fbe3c362b81c92",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot "
                "2026-09-28 (on-chain activity verified 2026-09-28)"),
    dict(eval_id="EVAL/2026/0047", address="1Lud76Q98VRHCUiyK7XUs7AgFofrqXeP78",
         chain="bitcoin", label="Ransomwhere: 7ev3n ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 "
                "(crowdsourced; on-chain activity verified via mempool.space "
                "2026-09-28)"),
    dict(eval_id="EVAL/2026/0048", address="1HyasSC2VifTZo7YkUNn33udnWXw3Ffq7T",
         chain="bitcoin", label="Ransomwhere: AES-NI ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 "
                "(crowdsourced; on-chain activity verified via mempool.space "
                "2026-09-28)"),
    dict(eval_id="EVAL/2026/0049", address="377CY1m8W2qbQQX5HHjziimdh2faGjDeLv",
         chain="bitcoin", label="Ransomwhere: APT ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 "
                "(crowdsourced; on-chain activity verified via mempool.space "
                "2026-09-28)"),
    dict(eval_id="EVAL/2026/0050", address="bc1qhzd63mz9mfucak7yzfn65p6rcsgztnsqr3dak8",
         chain="bitcoin", label="Ransomwhere: Akira ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 "
                "(crowdsourced; on-chain activity verified via mempool.space "
                "2026-09-28)"),
    dict(eval_id="EVAL/2026/0051", address="1DUBrMcH9T13oFSa59jxtFDM5eWTP8v2yc",
         chain="bitcoin", label="Ransomwhere: Ako ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 "
                "(crowdsourced; on-chain activity verified via mempool.space "
                "2026-09-28)"),
    dict(eval_id="EVAL/2026/0052", address="bc1qy2fx4vdnka3z264vdlg4qu88exvx8hlj5vvju5",
         chain="bitcoin", label="Ransomwhere: AlbDecryptor ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 "
                "(crowdsourced; on-chain activity verified via mempool.space "
                "2026-09-28)"),
    dict(eval_id="EVAL/2026/0053", address="bc1q65f238kv6gc235smuzcehshxcqljn2g7l5sz7j",
         chain="bitcoin", label="Ransomwhere: Avaddon ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 "
                "(crowdsourced; on-chain activity verified via mempool.space "
                "2026-09-28)"),
    dict(eval_id="EVAL/2026/0054", address="1MiGyooKN32XiiQ37x6EJwbbfw6mJ7d27G",
         chain="bitcoin", label="Ransomwhere: AvosLocker ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 "
                "(crowdsourced; on-chain activity verified via mempool.space "
                "2026-09-28)"),
    dict(eval_id="EVAL/2026/0055", address="bc1qnurh904jcnxm0amfg2cy3406k4ed2vd2x67s8p",
         chain="bitcoin", label="Ransomwhere: Bagli ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 "
                "(crowdsourced; on-chain activity verified via mempool.space "
                "2026-09-28)"),
    dict(eval_id="EVAL/2026/0056", address="17rJmFiKyYbNZmt9xiz8yTScX1QvWpt7pz",
         chain="bitcoin", label="Ransomwhere: Bitpaymer / DoppelPaymer ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 "
                "(crowdsourced; on-chain activity verified via mempool.space "
                "2026-09-28)"),
    dict(eval_id="EVAL/2026/0057", address="13rhLTYUKo9ijrR8vinojZqoZTpTe1fm8c",
         chain="bitcoin", label="Ransomwhere: Black Basta ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 "
                "(crowdsourced; on-chain activity verified via mempool.space "
                "2026-09-28)"),
    dict(eval_id="EVAL/2026/0058", address="1Lf8ZzcEhhRiXpk6YNQFpCJcUisiXb34FT",
         chain="bitcoin", label="Ransomwhere: Black Kingdom ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 "
                "(crowdsourced; on-chain activity verified via mempool.space "
                "2026-09-28)"),
    dict(eval_id="EVAL/2026/0059", address="1JjKYDsYrJGPCzLGGmFL8nM7AvUncd2wYW",
         chain="bitcoin", label="Ransomwhere: Black Mamba ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 "
                "(crowdsourced; on-chain activity verified via mempool.space "
                "2026-09-28)"),
    dict(eval_id="EVAL/2026/0060", address="19S7k3zHphKiYr85T25FnqdxizHcgmjoj1",
         chain="bitcoin", label="Ransomwhere: Black Ruby ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 "
                "(crowdsourced; on-chain activity verified via mempool.space "
                "2026-09-28)"),
    dict(eval_id="EVAL/2026/0061", address="14Q5xgBHAkWxDVrnHautcm4PPGmy5cfw6b",
         chain="bitcoin", label="Ransomwhere: BlackCat ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 "
                "(crowdsourced; on-chain activity verified via mempool.space "
                "2026-09-28)"),
    dict(eval_id="EVAL/2026/0062", address="bc1q2855268hg3lm34qwk5jvnnjm762ef8rkdvyjez",
         chain="bitcoin", label="Ransomwhere: BlackMatter ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 "
                "(crowdsourced; on-chain activity verified via mempool.space "
                "2026-09-28)"),
    dict(eval_id="EVAL/2026/0063", address="3BaS629MFciJ5cJKHSg4A5vncVok5Hxw7H",
         chain="bitcoin", label="Ransomwhere: BlackRouter ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 "
                "(crowdsourced; on-chain activity verified via mempool.space "
                "2026-09-28)"),
    dict(eval_id="EVAL/2026/0064", address="bc1q0c03s0c80uuxjq4jcyfhs4k8w5wu6ca9xhxsw9",
         chain="bitcoin", label="Ransomwhere: BlackSuit ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 "
                "(crowdsourced; on-chain activity verified via mempool.space "
                "2026-09-28)"),
    dict(eval_id="EVAL/2026/0065", address="1MfVk1utxgvGjMFV3K3CzXsDRDZznj5tey",
         chain="bitcoin", label="Ransomwhere: Bucbi ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 "
                "(crowdsourced; on-chain activity verified via mempool.space "
                "2026-09-28)"),
    dict(eval_id="EVAL/2026/0066", address="1Eh4f3p2fQVjfyHAyJ2rCqjUgDxPgjJE5q",
         chain="bitcoin", label="Ransomwhere: ChupaCabra ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 "
                "(crowdsourced; on-chain activity verified via mempool.space "
                "2026-09-28)"),
]

# --------------------------------------------------------------- watches
# classification is operator-set vocabulary (suspect|terminal|
# counterparty|vasp|general) — never inferred by the engine.
WATCHES: list[dict] = [
    dict(address="0xbf28eb4e017e472f9f6c11efabcdcb97f5761707", chain="ethereum",
         label="eval: ScamSniffer suspect", classification="suspect",
         case_eval_id="EVAL/2026/0001"),
    dict(address="0x22af344920a302ae1edfc8bfd10a44d395723e46", chain="ethereum",
         label="eval: ScamSniffer suspect", classification="suspect",
         case_eval_id="EVAL/2026/0002"),
    dict(address="0xe5100c85dc221758d9ceba53cc0e097ccb836ed0", chain="ethereum",
         label="eval: ScamSniffer suspect", classification="suspect",
         case_eval_id="EVAL/2026/0004"),
    dict(address="17TMc2UkVRSga2yYvuxSD9Q1XyB2EPRjTF", chain="bitcoin",
         label="eval: Netwalker ransom wallet", classification="suspect",
         case_eval_id="EVAL/2026/0009"),
    dict(address="1DUBrMcH9T13oFSa59jxtFDM5eWTP8v2yc", chain="bitcoin",
         label="eval: Ako ransom wallet", classification="suspect",
         case_eval_id="EVAL/2026/0010"),
    dict(address="1FEsU4nL3WBG4YWmzR9BwKtdn9ALqobWJ3", chain="bitcoin",
         label="eval: HC6/HC7 ransom wallet", classification="suspect",
         case_eval_id="EVAL/2026/0011"),
    dict(address="0xb97ef6609fb8b61611c1bcdb476a01af229cd617", chain="ethereum",
         label="eval: Tornado 1 ETH depositor", classification="suspect",
         case_eval_id="EVAL/2026/0015"),
    dict(address="0x9f5b7528dd288f4058672e9f6134e43857a009c3", chain="ethereum",
         label="eval: Tornado 1 ETH depositor", classification="suspect",
         case_eval_id="EVAL/2026/0016"),
    dict(address="0xA96Be652A08D9905F15B7FbE2255708709BeCD09", chain="ethereum",
         label="eval: ChangeNOW Hot Wallet 2 (Etherscan label)",
         classification="counterparty", case_eval_id=None),
    dict(address="0x4E5B2e1dc63F6b91cb6Cd759936495434C7e972F", chain="ethereum",
         label="eval: FixedFloat Hot Wallet 2 (Etherscan label)",
         classification="counterparty", case_eval_id=None),
    # M28 bulk additions — new-case suspects across both chains.
    dict(address="0xe10da2c1edac5e2061b19b3504a7608142d96f59", chain="ethereum",
         label="eval: ScamSniffer suspect (bulk)", classification="suspect",
         case_eval_id="EVAL/2026/0017"),
    dict(address="0x0000098a312e1244f313f83cac319603a97f4582", chain="ethereum",
         label="eval: ScamSniffer suspect (bulk)", classification="suspect",
         case_eval_id="EVAL/2026/0018"),
    dict(address="0x08a66c51e2d16a44c91592c8d5e62ef94bcbf4aa", chain="ethereum",
         label="eval: ScamSniffer suspect (bulk)", classification="suspect",
         case_eval_id="EVAL/2026/0019"),
    dict(address="0xe0ce8577cbe16e11cac4158ec62b9f8a561c3616", chain="ethereum",
         label="eval: ScamSniffer suspect (bulk)", classification="suspect",
         case_eval_id="EVAL/2026/0020"),
    dict(address="1Lud76Q98VRHCUiyK7XUs7AgFofrqXeP78", chain="bitcoin",
         label="eval: 7ev3n ransom wallet (bulk)", classification="suspect",
         case_eval_id="EVAL/2026/0047"),
    dict(address="1HyasSC2VifTZo7YkUNn33udnWXw3Ffq7T", chain="bitcoin",
         label="eval: AES-NI ransom wallet (bulk)", classification="suspect",
         case_eval_id="EVAL/2026/0048"),
    dict(address="377CY1m8W2qbQQX5HHjziimdh2faGjDeLv", chain="bitcoin",
         label="eval: APT ransom wallet (bulk)", classification="suspect",
         case_eval_id="EVAL/2026/0049"),
    dict(address="bc1qhzd63mz9mfucak7yzfn65p6rcsgztnsqr3dak8", chain="bitcoin",
         label="eval: Akira ransom wallet (bulk)", classification="suspect",
         case_eval_id="EVAL/2026/0050"),
]

# ----------------------------------------------------------- demo users
# Operator accounts so the admin console has real rows. Raw keys are
# printed ONCE by the seed — rotate them after the evaluation.
DEMO_USERS: list[dict] = [
    dict(name="eval.analyst", email="eval-analyst@example.invalid",
         role="analyst", jurisdictions=["IN"]),
    dict(name="eval.viewer", email="eval-viewer@example.invalid",
         role="viewer", jurisdictions=["IN"]),
]

MAX_CONCURRENCY = 3
MAX_ATTEMPTS = 3

# Cases the engine must not trace: their on-chain neighborhood fans out
# beyond practical bounds (60+ sequential indexer calls, no completion
# within the trace timeout). They are attributed directly from their
# public threat-feed source by scripts/feed_attribute_0047.py instead,
# with the methodology labeled honestly as feed-attributed.
_FEED_ATTRIBUTED = {"EVAL/2026/0047"}
# M28: baseline tick learns only the N most recent txs; the catch-up
# tick runs the normal window so the address's real recent history
# surfaces as the watch's initial alerts (documented in the module
# docstring — every alerted tx is genuinely on-chain).
BASELINE_LIMIT = 5


async def _find_case(store, eval_id):
    recs, _ = await store.list_cases(search=eval_id, limit=50)
    for r in recs:
        if r.fir_number == eval_id:
            return r
    return None


async def _seed_case(ctx, spec, sem):
    from worker import trace_wallet

    store = ctx["store"]
    async with sem:
        case = await _find_case(store, spec["eval_id"])
        if case is None:
            from engine.store.base import CaseIn
            case = await store.create_case(CaseIn(
                fir_number=spec["eval_id"],
                suspect_address=spec["address"],
                chain=spec["chain"],
                officer_id="evaluation-seed",
                notes=f"{spec['label']}. Source: {spec['source']}. {EVAL}"))
            print(f"[{spec['eval_id']}] case created", flush=True)
        else:
            print(f"[{spec['eval_id']}] case exists", flush=True)

        if await store.get_report_by_case(case.id) is not None:
            print(f"[{spec['eval_id']}] report exists — skipping trace",
                  flush=True)
            return ("skipped", spec["eval_id"])

        if spec["eval_id"] in _FEED_ATTRIBUTED:
            print(f"[{spec['eval_id']}] feed-attributed case — skipping "
                  f"engine trace (run scripts/feed_attribute_0047.py)",
                  flush=True)
            return ("skipped", spec["eval_id"])

        last_err = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            job = await store.create_job(case.id, spec["address"],
                                         spec["chain"])
            try:
                # One slow/hung trace must not stall the whole bulk run.
                out = await asyncio.wait_for(trace_wallet(
                    ctx, job_id=str(job.id), case_id=str(case.id),
                    address=spec["address"], chain=spec["chain"]),
                    timeout=600)
                report = await store.get_report(UUID(out["report_id"]))
                print(f"[{spec['eval_id']}] traced: risk={report.risk_score} "
                      f"({report.risk_level}) conf={report.confidence} "
                      f"terminal={report.terminal_reason} "
                      f"webhook_ok={out['webhook_ok']}", flush=True)
                return ("traced", spec["eval_id"])
            except Exception as exc:  # noqa: BLE001 — retried, then reported
                last_err = f"{type(exc).__name__}: {exc}"
                print(f"[{spec['eval_id']}] attempt {attempt} failed: "
                      f"{last_err}", flush=True)
                # Ensure the job is marked failed (CancelledError from
                # wait_for timeout bypasses the worker's except Exception).
                try:
                    await store.set_job(
                        job.id, "failed",
                        error=f"seed timeout/cancelled: {last_err}")
                except Exception:
                    pass
                await asyncio.sleep(10 * attempt)
        print(f"[{spec['eval_id']}] FAILED after {MAX_ATTEMPTS} attempts: "
              f"{last_err}", flush=True)
        return ("failed", spec["eval_id"])


async def _seed_watches(ctx, case_ids: dict[str, UUID]):
    from datetime import datetime, timezone

    from engine.jobs.pipeline import make_adapter
    from engine.store.base import WatchCheckRec, WatchIn
    from engine.watch.watcher import check_watch, process_watch
    from api.core.config import settings

    store = ctx["store"]
    existing = await store.list_watches(active_only=False)
    known = {(w.address.lower(), w.chain) for w in existing}
    alert_url = (settings.sahyog_mock_url.rstrip("/")
                 + "/sahyog/webhook/watch-alert")
    n_dispositioned = 0
    for spec in WATCHES:
        key = (spec["address"].lower(), spec["chain"])
        if key in known:
            print(f"[watch] exists: {spec['label']}", flush=True)
            continue
        rec = await store.add_watch(WatchIn(
            address=spec["address"], chain=spec["chain"],
            label=spec["label"], classification=spec["classification"],
            case_id=case_ids.get(spec["case_eval_id"] or ""),
            created_by="evaluation-seed"))
        try:
            # tick 1 — baseline from the N most recent txs only.
            # Bounded: one slow indexer must not stall the whole seed.
            watch = await store.get_watch(rec.id)
            base = await asyncio.wait_for(
                check_watch(watch, make_adapter, limit=BASELINE_LIMIT),
                timeout=120)
            await store.set_watch(
                rec.id, watch.status, seen_hashes=base.seen_hashes,
                last_checked_at=base.checked_at)
            await store.record_watch_check(WatchCheckRec(
                id=uuid4(), watch_id=rec.id, checked_at=base.checked_at,
                txs_seen=base.txs_examined, baseline=True))
            # tick 2 — normal window: the address's real recent history
            # beyond the baseline surfaces as catch-up alerts.
            # Bounded: one slow indexer must not stall the whole seed.
            watch = await store.get_watch(rec.id)
            out = await asyncio.wait_for(
                process_watch(
                    store, watch, adapter_factory=make_adapter,
                    alert_url=alert_url,
                    secret=settings.engine_webhook_secret),
                timeout=120)
            n_events = len(out["events"])
            print(f"[watch] created + 2 ticks: {spec['label']} "
                  f"(catch-up alerts: {n_events})", flush=True)
            # simulated analyst review on a couple of alerts, labeled.
            if n_dispositioned < 4:
                alerts = await store.list_alerts(rec.id)
                for a in alerts[:2]:
                    await store.set_alert_disposition(
                        a.id, "benign",
                        "EVALUATION SEED — simulated analyst review: "
                        "historical movement, no illicit context "
                        "established in this evaluation.",
                        by="evaluation-seed")
                    n_dispositioned += 1
        except Exception as exc:  # noqa: BLE001 — watch exists regardless
            print(f"[watch] created, tick failed: {spec['label']}: "
                  f"{type(exc).__name__}: {exc}", flush=True)
    print(f"[watch] simulated dispositions: {n_dispositioned}")


async def _seed_users(ctx):
    """Demo operator accounts so the admin console has real rows."""
    from engine.auth import hash_key, new_api_key
    from engine.store.base import ApiUserIn

    store = ctx["store"]
    existing = {u.name for u in await store.list_users()}
    for spec in DEMO_USERS:
        if spec["name"] in existing:
            print(f"[user] exists: {spec['name']}", flush=True)
            continue
        raw_key = new_api_key()
        await store.create_user(
            ApiUserIn(name=spec["name"], email=spec["email"],
                      role=spec["role"],
                      jurisdictions=spec["jurisdictions"]),
            key_hash=hash_key(raw_key))
        print(f"[user] created: {spec['name']} ({spec['role']}) "
              f"key={raw_key}  <-- DEMO ONLY, rotate after evaluation",
              flush=True)


async def _seed_feedback(ctx, case_ids: dict[str, UUID]):
    """Honest review placeholders: 'inconclusive' outcomes for a sample
    of traced cases — no real-world confirmation was sought, and the
    record says so. Populates the feedback console without inventing
    confirmations."""
    from engine.store.base import FeedbackOutcomeIn

    store = ctx["store"]
    existing = await store.list_outcomes()
    if existing:
        print(f"[feedback] {len(existing)} outcomes already recorded — "
              "skipping", flush=True)
        return
    sample = ["EVAL/2026/0001", "EVAL/2026/0009", "EVAL/2026/0015",
              "EVAL/2026/0017", "EVAL/2026/0047", "EVAL/2026/0025",
              "EVAL/2026/0055", "EVAL/2026/0033"]
    n = 0
    for eval_id in sample:
        cid = case_ids.get(eval_id)
        if cid is None:
            continue
        report = await store.get_report_by_case(cid)
        if report is None:
            continue
        await store.record_outcome(
            FeedbackOutcomeIn(
                case_id=cid,
                vasp=(report.terminal_address or "unresolved — see report"),
                predicted_confidence=report.confidence or 0.0,
                outcome="inconclusive",
                notes="EVALUATION SEED — no real-world confirmation was "
                      "sought for this evaluation case; placeholder for "
                      "the analyst review workflow."),
            recorded_by="evaluation-seed")
        n += 1
    print(f"[feedback] recorded {n} inconclusive evaluation outcomes")


async def _amain(args) -> int:
    from api.core.config import settings
    from engine.graph import get_graph_store
    from engine.store import init_store
    from engine.store.memory import MemoryStore
    from worker import _load_sanctions, _load_threat_feeds

    wanted = set(args.cases.split(",")) if args.cases else None
    specs = [c for c in CASES if wanted is None or c["eval_id"] in wanted]
    if not specs:
        print("no cases match --cases filter")
        return 1

    if args.dry_run:
        print(f"plan (dry run — nothing written, no infra needed): "
              f"{len(specs)} cases, {len(WATCHES)} watches, "
              f"{len(DEMO_USERS)} users")
        for c in specs:
            print(f"  {c['eval_id']}  {c['chain']:9s} {c['address']}  "
                  f"{c['label']}")
        for w in WATCHES:
            print(f"  watch   {w['chain']:9s} {w['address']}  {w['label']}")
        return 0

    t0 = time.time()
    store = await init_store(settings)
    if isinstance(store, MemoryStore):
        print("REFUSING: the store backend is memory (process-local) — a "
              "seed written here would be invisible to the API. Start "
              "Postgres (docker compose up -d) or set STORE_BACKEND=postgres "
              "with DATABASE_URL pointing at a live database.")
        return 2
    print(f"store: {type(store).__name__} (shared with the API)")

    # M32 preflight: the seed calls trace_wallet in-process, which POSTs
    # every attribution to the SAHYOG mock. If the mock isn't reachable,
    # all 41 filings fail delivery silently — catch it in 5 seconds here
    # instead of discovering webhook_ok=False after a 3-hour run.
    _mock_url = settings.sahyog_mock_url.rstrip("/")
    try:
        import httpx
        _r = httpx.get(_mock_url + "/sahyog/webhooks", timeout=5.0)
        _r.raise_for_status()
        print(f"SAHYOG mock: reachable at {_mock_url}")
    except Exception as exc:  # noqa: BLE001 — preflight must not fail seed
        print(f"WARNING: SAHYOG mock NOT reachable at {_mock_url} ({exc}). "
              f"Continuing, but every filing's webhook delivery will fail "
              f"(webhook_ok=False). Start it with: docker compose up -d "
              f"sahyog-mock")

    graph_store = get_graph_store()
    print(f"graph store: {graph_store.backend}"
          + ("" if graph_store.backend == "neo4j"
             else " — graph pages will stay EMPTY until Neo4j is up"))

    ctx = {
        "settings": settings,
        "store": store,
        "sanctions": _load_sanctions(settings),
        "threat_feeds": _load_threat_feeds(),
        "graph_store": graph_store,
    }

    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    results = await asyncio.gather(
        *(_seed_case(ctx, c, sem) for c in specs))

    case_ids: dict[str, UUID] = {}
    for c in specs:
        case = await _find_case(store, c["eval_id"])
        if case is not None:
            case_ids[c["eval_id"]] = case.id
    await _seed_watches(ctx, case_ids)
    await _seed_users(ctx)
    await _seed_feedback(ctx, case_ids)

    n_traced = sum(1 for s, _ in results if s == "traced")
    n_skipped = sum(1 for s, _ in results if s == "skipped")
    n_failed = sum(1 for s, _ in results if s == "failed")
    print(f"done in {time.time() - t0:.0f}s: {n_traced} traced, "
          f"{n_skipped} skipped (already seeded), {n_failed} failed")

    # Population quality summary: transactions / hops / topology per
    # case, so the run is audited on data richness, not just counts.
    try:
        from scripts.population_report import (
            collect_population, format_summary)
        _, pop = await collect_population(store, graph_store)
        print("population quality:")
        print(format_summary(pop), end="")
    except Exception as exc:  # noqa: BLE001 — summary never fails seed
        print(f"(population summary unavailable: {exc})")

    engine = getattr(store, "engine", None)
    if engine is not None:
        await engine.dispose()
    await graph_store.close()
    return 1 if n_failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--yes", action="store_true",
                    help="actually write to the database")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the plan without writing anything")
    ap.add_argument("--cases", default="",
                    help="comma-separated eval IDs to seed "
                         "(default: all 66)")
    args = ap.parse_args()
    if not args.yes and not args.dry_run \
            and os.environ.get("SEED_DEMO") != "1":
        print(__doc__)
        print("refusing: this writes to your database — pass --yes, "
              "--dry-run, or SEED_DEMO=1")
        return 2
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
