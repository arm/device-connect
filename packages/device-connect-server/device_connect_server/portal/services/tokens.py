# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Bearer token issuance + verification for the agent API.

Tokens are stored in etcd under /device-connect/portal/tokens/{token_id}.
Only the SHA-256 hash of the token secret is persisted; the secret itself
is returned exactly once at creation time and is never recoverable.

Token wire format: "dcp_{token_id}_{secret}"
- token_id: 16-hex-char random id, used as the etcd lookup key
- secret:   32-byte urlsafe-base64 random string, only its hash is stored
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timezone

from .. import config

logger = logging.getLogger(__name__)

_TOKENS_PREFIX = "/device-connect/portal/tokens/"

TOKEN_PREFIX = "dcp_"

# All scopes recognized by the agent API. Anything else is rejected at create-time.
KNOWN_SCOPES = frozenset({
    "devices:read",
    "devices:provision",
    "devices:credentials",
    "devices:invoke",
    "events:read",
    "admin:tenants",
    "admin:*",
})


class TokenError(Exception):
    """Raised when a token operation fails."""


def _etcd_client():
    from etcd3gw import Etcd3Client
    return Etcd3Client(host=config.ETCD_HOST, port=config.ETCD_PORT)


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_token(token: str) -> tuple[str, str] | None:
    """Split a wire-format token into (token_id, secret). Returns None on malformed input."""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    rest = token[len(TOKEN_PREFIX):]
    parts = rest.split("_", 1)
    if len(parts) != 2:
        return None
    token_id, secret = parts
    if not token_id or not secret:
        return None
    return token_id, secret


def validate_scopes(scopes: list[str]) -> list[str]:
    """Return a sorted, de-duped, validated scope list. Raises ValueError on unknown."""
    cleaned = []
    seen = set()
    for s in scopes:
        s = s.strip()
        if not s or s in seen:
            continue
        if s not in KNOWN_SCOPES:
            raise ValueError(f"Unknown scope: {s}")
        cleaned.append(s)
        seen.add(s)
    cleaned.sort()
    return cleaned


def create_token(
    *,
    username: str,
    tenant: str,
    role: str,
    scopes: list[str],
    label: str = "",
    expires_at: str | None = None,
) -> dict:
    """Mint a new token. Returns dict with full token secret in 'token' field.

    The secret is only returned here. Subsequent reads via get_token_record /
    list_tokens never expose it.
    """
    scopes = validate_scopes(scopes)

    token_id = secrets.token_hex(8)  # 16 hex chars
    secret = secrets.token_urlsafe(32)
    secret_hash = _hash_secret(secret)

    record = {
        "token_id": token_id,
        "username": username,
        "tenant": tenant,
        "role": role,
        "scopes": scopes,
        "label": label,
        "secret_hash": secret_hash,
        "created_at": _now_iso(),
        "expires_at": expires_at,
        "revoked": False,
    }

    client = _etcd_client()
    client.put(_TOKENS_PREFIX + token_id, json.dumps(record))

    full_token = f"{TOKEN_PREFIX}{token_id}_{secret}"
    logger.info("Created agent token %s for user=%s tenant=%s scopes=%s",
                token_id, username, tenant, scopes)

    out = dict(record)
    out.pop("secret_hash", None)
    out["token"] = full_token  # only present on creation
    return out


def get_token_record(token_id: str) -> dict | None:
    """Fetch a token record by id. Excludes secret_hash from the returned dict."""
    client = _etcd_client()
    values = client.get(_TOKENS_PREFIX + token_id)
    if not values:
        return None
    raw = values[0]
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        record = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    record.pop("secret_hash", None)
    return record


def _get_token_record_with_hash(token_id: str) -> dict | None:
    """Internal: fetch token record including secret_hash for verification."""
    client = _etcd_client()
    values = client.get(_TOKENS_PREFIX + token_id)
    if not values:
        return None
    raw = values[0]
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def list_tokens(username: str | None = None, tenant: str | None = None) -> list[dict]:
    """List token records. Excludes secret_hash. Filters by user/tenant if given."""
    client = _etcd_client()
    out = []
    for raw, _meta in client.get_prefix(_TOKENS_PREFIX):
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if username and record.get("username") != username:
            continue
        if tenant and record.get("tenant") != tenant:
            continue
        record.pop("secret_hash", None)
        out.append(record)
    out.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return out


def revoke_token(token_id: str) -> bool:
    """Mark a token as revoked. Returns True if it existed."""
    client = _etcd_client()
    values = client.get(_TOKENS_PREFIX + token_id)
    if not values:
        return False
    raw = values[0]
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        record = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    record["revoked"] = True
    record["revoked_at"] = _now_iso()
    client.put(_TOKENS_PREFIX + token_id, json.dumps(record))
    logger.info("Revoked agent token %s", token_id)
    return True


def verify_token(token: str) -> dict | None:
    """Verify a wire-format token. Returns the record (without secret_hash) on success.

    Returns None on any failure: malformed, unknown id, hash mismatch, revoked,
    or expired. Constant-time secret comparison.
    """
    parsed = _parse_token(token)
    if not parsed:
        return None
    token_id, secret = parsed

    record = _get_token_record_with_hash(token_id)
    if not record:
        return None

    expected = record.get("secret_hash", "")
    if not expected:
        return None
    if not hmac.compare_digest(expected, _hash_secret(secret)):
        return None

    if record.get("revoked"):
        return None

    expires_at = record.get("expires_at")
    if expires_at:
        try:
            exp = datetime.fromisoformat(expires_at)
            if exp <= datetime.now(timezone.utc):
                return None
        except (ValueError, TypeError):
            return None

    record.pop("secret_hash", None)
    return record


def has_scope(record: dict, required: str) -> bool:
    """Return True if the token holds the required scope (or admin:* wildcard)."""
    scopes = record.get("scopes", []) or []
    if required in scopes:
        return True
    if required.startswith("admin:") and "admin:*" in scopes:
        return True
    return False
