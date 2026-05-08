# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Browser-mediated CLI auth flow (device authorization grant).

The CLI calls `init` (no auth) to get a `request_id` and prints a verification
URL the user opens in their browser. After login, the user clicks Approve;
the portal mints a scoped token bound to the logged-in user and attaches it
to the pending record. The CLI's `poll` returns the token and the record is
consumed (deleted) on first successful read.

Records live at /device-connect/portal/cli_auth/{request_id} in etcd.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

from .. import config
from . import tokens as tokens_svc

logger = logging.getLogger(__name__)

_PREFIX = "/device-connect/portal/cli_auth/"

# Status enum
PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"

# Lifetimes
DEFAULT_TTL_SECONDS = 600           # 10 min — user has this long to click approve
POLL_INTERVAL_SECONDS = 3

# Scopes a regular (non-admin) user is allowed to grant via the browser flow.
REGULAR_SCOPES = frozenset({
    "devices:read",
    "devices:provision",
    "devices:credentials",
    "devices:invoke",
    "events:read",
})


class CliAuthError(Exception):
    """Raised when a CLI auth operation fails."""


def _etcd_client():
    from etcd3gw import Etcd3Client
    return Etcd3Client(host=config.ETCD_HOST, port=config.ETCD_PORT)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expires_in_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _is_expired(record: dict) -> bool:
    exp = record.get("expires_at")
    if not exp:
        return False
    try:
        return datetime.fromisoformat(exp) <= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return True


def init(*, scopes_requested: list[str], label: str = "",
         ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict:
    """Create a pending CLI-auth request. Returns the public-facing record."""
    # Validate up-front so the CLI fails before printing a URL.
    try:
        cleaned = tokens_svc.validate_scopes(scopes_requested)
    except ValueError as e:
        raise CliAuthError(str(e))
    if not cleaned:
        raise CliAuthError("scopes_requested must include at least one scope")

    request_id = secrets.token_hex(16)  # 32 hex chars
    record = {
        "request_id": request_id,
        "scopes_requested": cleaned,
        "label": label or "",
        "status": PENDING,
        "created_at": _now_iso(),
        "expires_at": _expires_in_iso(ttl_seconds),
        "approved_by": None,
        "tenant": None,
        "scopes_granted": None,
        "token": None,            # full wire-format token, returned ONCE then consumed
        "token_id": None,
    }
    client = _etcd_client()
    client.put(_PREFIX + request_id, json.dumps(record))
    logger.info("CLI auth init: request_id=%s scopes=%s label=%s",
                request_id, cleaned, label)
    return {"request_id": request_id,
            "scopes_requested": cleaned,
            "label": label,
            "expires_at": record["expires_at"],
            "poll_interval": POLL_INTERVAL_SECONDS}


def get(request_id: str) -> dict | None:
    """Fetch a record by id. Excludes the token field."""
    record = _get_full(request_id)
    if record is None:
        return None
    public = dict(record)
    public.pop("token", None)
    return public


def _get_full(request_id: str) -> dict | None:
    client = _etcd_client()
    values = client.get(_PREFIX + request_id)
    if not values:
        return None
    raw = values[0]
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _put(record: dict) -> None:
    client = _etcd_client()
    client.put(_PREFIX + record["request_id"], json.dumps(record))


def _delete(request_id: str) -> None:
    client = _etcd_client()
    try:
        client.delete(_PREFIX + request_id)
    except Exception:
        # etcd3gw delete API varies between versions — best effort
        try:
            client.delete_range(_PREFIX + request_id)  # type: ignore[attr-defined]
        except Exception:
            logger.debug("cli_auth: failed to delete %s", request_id, exc_info=True)


def _clamp_scopes(requested: list[str], role: str) -> list[str]:
    if role == "admin":
        return list(requested)
    return [s for s in requested if s in REGULAR_SCOPES]


def approve(*, request_id: str, user: dict, scopes_granted: list[str] | None = None) -> dict:
    """Mark a pending record approved and mint a token bound to the user.

    `user` must contain at least: username, tenant, role.
    `scopes_granted` defaults to the originally requested scopes, clamped by
    what the approving user is allowed to grant given their role.
    """
    record = _get_full(request_id)
    if record is None:
        raise CliAuthError("request_id not found")
    if record["status"] != PENDING:
        raise CliAuthError(f"request is already {record['status']}")
    if _is_expired(record):
        record["status"] = "expired"
        _put(record)
        raise CliAuthError("request has expired")

    requested = list(record.get("scopes_requested") or [])
    granted_in = list(scopes_granted) if scopes_granted is not None else requested
    granted = [s for s in granted_in if s in requested]
    granted = _clamp_scopes(granted, user.get("role", "user"))
    if not granted:
        raise CliAuthError("no scopes granted")

    minted = tokens_svc.create_token(
        username=user["username"],
        tenant=user["tenant"],
        role=user.get("role", "user"),
        scopes=granted,
        label=record.get("label") or f"cli-login {user['username']}",
    )
    record["status"] = APPROVED
    record["approved_by"] = user["username"]
    record["tenant"] = user["tenant"]
    record["scopes_granted"] = minted["scopes"]
    record["token"] = minted["token"]
    record["token_id"] = minted["token_id"]
    record["approved_at"] = _now_iso()
    _put(record)
    logger.info("CLI auth approved: request_id=%s by=%s scopes=%s",
                request_id, user["username"], minted["scopes"])
    return get(request_id)


def deny(*, request_id: str, user: dict | None = None) -> dict:
    record = _get_full(request_id)
    if record is None:
        raise CliAuthError("request_id not found")
    if record["status"] != PENDING:
        raise CliAuthError(f"request is already {record['status']}")
    record["status"] = DENIED
    if user:
        record["approved_by"] = user.get("username")
    record["denied_at"] = _now_iso()
    _put(record)
    logger.info("CLI auth denied: request_id=%s by=%s", request_id, user and user.get("username"))
    return get(request_id)


def consume_on_poll(request_id: str) -> tuple[str, dict | None]:
    """Poll a record. On success, return the wire-format token and DELETE the record.

    Returns (status, payload) where:
      - status="approved":  payload={token, scopes, username, tenant, token_id} — record deleted
      - status="pending":   payload=None
      - status="denied":    payload=None — record deleted
      - status="expired":   payload=None — record deleted
      - status="not_found": payload=None
    """
    record = _get_full(request_id)
    if record is None:
        return "not_found", None

    if record["status"] == APPROVED:
        payload = {
            "token": record["token"],
            "scopes": record["scopes_granted"],
            "username": record["approved_by"],
            "tenant": record["tenant"],
            "token_id": record["token_id"],
        }
        _delete(request_id)
        return "approved", payload

    if _is_expired(record):
        _delete(request_id)
        return "expired", None

    if record["status"] == DENIED:
        _delete(request_id)
        return "denied", None

    return "pending", None
