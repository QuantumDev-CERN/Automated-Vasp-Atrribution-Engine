"""CREATE2 / deposit-proxy knowledge (M16).

Many custodial exchanges issue each user a *deposit proxy*: a tiny contract
(EIP-1167 minimal proxy, or another forwarder) mass-deployed through a known
CREATE2 factory. Funds land on the proxy, then get swept to the exchange hot
wallet. Recognizing the proxy pattern keeps the engine from misreading the
deposit address as an unknown terminal — and lets sweep back-labeling tag it
as an exchange deposit address.

Two independent signals:
  1. Bytecode: the EIP-1167 minimal-proxy template (45 bytes, implementation
     address embedded). Pure pattern match on eth_getCode output.
  2. Provenance: the address was deployed via CREATE2 by a known factory
     (registry below). Computed as keccak(0xff ++ factory ++ salt ++
     keccak(init_code))[12:] per EIP-1014.
"""
from dataclasses import dataclass
from typing import Optional

from Crypto.Hash import keccak


# ---------------------------------------------------------------------------
# keccak-256 (Ethereum variant, NOT NIST SHA3-256)
# ---------------------------------------------------------------------------

def keccak256(data: bytes) -> bytes:
    """Ethereum keccak-256. Validated against the empty-string and
    Transfer-topic vectors in tests/test_m16.py."""
    return keccak.new(digest_bits=256, data=data).digest()


# ---------------------------------------------------------------------------
# EIP-1014 CREATE2 address derivation
# ---------------------------------------------------------------------------

def create2_address(deployer: str, salt: bytes, init_code: bytes) -> str:
    """Derive the address a CREATE2 deployment lands at.

    deployer: 0x-address of the contract executing CREATE2.
    salt: 32-byte salt. init_code: the full init bytecode (with any
    constructor args appended).
    """
    if len(salt) != 32:
        raise ValueError(f"CREATE2 salt must be 32 bytes, got {len(salt)}")
    deployer_bytes = bytes.fromhex(deployer.removeprefix("0x").removeprefix("0X"))
    if len(deployer_bytes) != 20:
        raise ValueError(f"not a 20-byte deployer address: {deployer}")
    digest = keccak256(
        b"\xff" + deployer_bytes + salt + keccak256(init_code)
    )
    return "0x" + digest[-20:].hex()


# ---------------------------------------------------------------------------
# EIP-1167 minimal proxy bytecode recognition
# ---------------------------------------------------------------------------

# Runtime bytecode template: 363d3d373d3d3d363d73<20-byte impl>5af43d82803e903d91602b57fd5bf3
EIP1167_PREFIX = "363d3d373d3d3d363d73"
EIP1167_SUFFIX = "5af43d82803e903d91602b57fd5bf3"
EIP1167_RUNTIME_LEN = 45  # bytes


def eip1167_implementation(code: str) -> Optional[str]:
    """If `code` (0x-prefixed hex runtime bytecode) is exactly the EIP-1167
    minimal-proxy template, return the embedded implementation address.
    Otherwise return None."""
    body = code.removeprefix("0x").removeprefix("0X").lower()
    if len(body) != EIP1167_RUNTIME_LEN * 2:
        return None
    if not (body.startswith(EIP1167_PREFIX) and body.endswith(EIP1167_SUFFIX)):
        return None
    try:
        bytes.fromhex(body)  # reject non-hex
    except ValueError:
        return None
    return "0x" + body[len(EIP1167_PREFIX):len(EIP1167_PREFIX) + 40]


# ---------------------------------------------------------------------------
# Known CREATE2 factory registry
# ---------------------------------------------------------------------------
# Every entry needs a named source and ideally an on-chain check (registry
# audit rule, M4). Addresses stored lowercase; lookup is case-insensitive.

CREATE2_FACTORIES: dict[str, str] = {
    # Arachnid's deterministic deployment proxy — the canonical keyless
    # CREATE2 deployer (same address on every EVM chain).
    # Source: https://github.com/Arachnid/deterministic-deployment-proxy
    # (also referenced by Optimism specs preinstalls.md and the Foundry book).
    # Verified on-chain 2026-09-27: eth_getCode on Ethereum mainnet returns
    # 69 bytes of code at this address.
    "0x4e59b44847b379578588920ca78fbf26c0b4956c":
        "arachnid-deterministic-deployment-proxy",
}


def create2_factory_name(address: str) -> Optional[str]:
    """Return the factory name if `address` is a known CREATE2 deployer."""
    return CREATE2_FACTORIES.get(address.lower())


# ---------------------------------------------------------------------------
# Combined detection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProxyInfo:
    kind: str  # eip1167-minimal-proxy | create2-deployed | eip1167-via-create2
    implementation: Optional[str] = None  # for EIP-1167 proxies
    factory: Optional[str] = None  # known CREATE2 factory name, when known


def detect_deposit_proxy(
    code: str, deployer: Optional[str] = None
) -> Optional[ProxyInfo]:
    """Decide whether contract bytecode (+ optional deployer) marks a
    deposit-proxy candidate.

    - EIP-1167 bytecode alone -> "eip1167-minimal-proxy"
    - known CREATE2 factory deployer alone -> "create2-deployed"
    - both -> "eip1167-via-create2"
    Returns None when neither signal fires.
    """
    impl = eip1167_implementation(code)
    factory = create2_factory_name(deployer) if deployer else None
    if impl is not None and factory is not None:
        return ProxyInfo(
            kind="eip1167-via-create2", implementation=impl, factory=factory
        )
    if impl is not None:
        return ProxyInfo(kind="eip1167-minimal-proxy", implementation=impl)
    if factory is not None:
        return ProxyInfo(kind="create2-deployed", factory=factory)
    return None
