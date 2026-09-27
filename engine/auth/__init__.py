"""M12: RBAC primitives.

API keys identify callers; only the SHA-256 hash is ever stored. Roles
imply capability sets (see VALID_ROLES / ROLE_CAPS in engine.store.base).
"""
from __future__ import annotations

import hashlib
import secrets


def new_api_key() -> str:
    """Generate a raw API key. Shown to the user exactly once."""
    return "vea_" + secrets.token_hex(24)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
