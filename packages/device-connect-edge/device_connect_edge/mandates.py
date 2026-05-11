# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Device Mandate helpers.

This module implements the first Device Mandate credential profile used by
Device Connect tests and demos. It is intentionally small and stdlib-only:
the public runtime contract is the mandate envelope and verifier interface,
while production credential formats can be added behind the same boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable


MANDATE_FORMAT = "device-connect-hmac-v0"


@dataclass(frozen=True)
class MandateInvocationContext:
    """Concrete invocation a closed mandate must authorize."""

    device_id: str
    method: str
    params: dict[str, Any]
    now: datetime | None = None


@dataclass(frozen=True)
class MandateVerificationResult:
    """Verifier result for expected allow/deny outcomes."""

    ok: bool
    error_code: str | None = None
    message: str = ""


KeyResolver = Callable[[str], bytes | str | None]


def create_open_mandate(
    *,
    principal: str,
    agent: str,
    device_id: str,
    methods: list[str],
    constraints: dict[str, Any] | None,
    not_before: datetime,
    not_after: datetime,
    key: bytes | str,
    mandate_id: str | None = None,
) -> dict[str, Any]:
    """Create and sign an open mandate."""

    payload = {
        "id": mandate_id or f"open-{uuid.uuid4().hex[:12]}",
        "principal": principal,
        "agent": agent,
        "device_id": device_id,
        "methods": list(methods),
        "constraints": constraints or {},
        "not_before": _format_dt(not_before),
        "not_after": _format_dt(not_after),
    }
    return {**payload, "signature": _sign(payload, key)}


def create_closed_mandate(
    *,
    open_mandate: dict[str, Any],
    agent: str,
    device_id: str,
    method: str,
    params: dict[str, Any],
    key: bytes | str,
    issued_at: datetime,
    mandate_id: str | None = None,
    nonce: str | None = None,
) -> dict[str, Any]:
    """Create and sign a closed mandate for one concrete invocation."""

    payload = {
        "format": MANDATE_FORMAT,
        "id": mandate_id or f"closed-{uuid.uuid4().hex[:12]}",
        "agent": agent,
        "open_mandate": open_mandate,
        "invocation": {
            "device_id": device_id,
            "method": method,
            "params": params,
        },
        "issued_at": _format_dt(issued_at),
        "nonce": nonce or uuid.uuid4().hex,
    }
    return {**payload, "signature": _sign(payload, key)}


def verify_mandate(
    mandate: dict[str, Any] | None,
    *,
    context: MandateInvocationContext,
    key_resolver: KeyResolver,
    replay_cache: set[str] | None = None,
) -> MandateVerificationResult:
    """Verify that a closed mandate authorizes an invocation."""

    if not mandate:
        return _deny("mandate_required", "mandate_required: protected RPC needs a mandate")
    if not isinstance(mandate, dict):
        return _deny("invalid_mandate", "invalid_mandate: mandate must be an object")
    if mandate.get("format") != MANDATE_FORMAT:
        return _deny("invalid_mandate", "invalid_mandate: unsupported mandate format")

    open_mandate = mandate.get("open_mandate")
    if not isinstance(open_mandate, dict):
        return _deny("invalid_mandate", "invalid_mandate: missing open mandate")

    principal = open_mandate.get("principal")
    agent = mandate.get("agent")
    if not isinstance(principal, str) or not isinstance(agent, str):
        return _deny("invalid_mandate", "invalid_mandate: missing principal or agent")
    if open_mandate.get("agent") != agent:
        return _deny("mandate_agent_denied", "mandate_agent_denied: agent mismatch")

    principal_key = key_resolver(principal)
    agent_key = key_resolver(agent)
    if principal_key is None or agent_key is None:
        return _deny("unknown_mandate_key", "unknown_mandate_key: signer key unavailable")
    if not _signature_valid(open_mandate, principal_key):
        return _deny("invalid_mandate_signature", "invalid_mandate_signature: open mandate")
    if not _signature_valid(mandate, agent_key):
        return _deny("invalid_mandate_signature", "invalid_mandate_signature: closed mandate")

    now = _as_utc(context.now or datetime.now(timezone.utc))
    not_before = _parse_dt(str(open_mandate.get("not_before", "")))
    not_after = _parse_dt(str(open_mandate.get("not_after", "")))
    if not_before is None or not_after is None:
        return _deny("invalid_mandate", "invalid_mandate: invalid validity window")
    if now < not_before:
        return _deny("mandate_not_yet_valid", "mandate_not_yet_valid")
    if now > not_after:
        return _deny("mandate_expired", "mandate_expired")

    if open_mandate.get("device_id") != context.device_id:
        return _deny("mandate_device_denied", "mandate_device_denied")
    if context.method not in (open_mandate.get("methods") or []):
        return _deny("mandate_method_denied", "mandate_method_denied")

    invocation = mandate.get("invocation") or {}
    if invocation.get("device_id") != context.device_id:
        return _deny("mandate_device_denied", "mandate_device_denied")
    if invocation.get("method") != context.method:
        return _deny("mandate_method_denied", "mandate_method_denied")
    if invocation.get("params") != context.params:
        return _deny("mandate_params_denied", "mandate_params_denied")

    constraint_error = _check_constraints(
        open_mandate.get("constraints") or {}, context.params,
    )
    if constraint_error is not None:
        return _deny("mandate_constraint_denied", constraint_error)

    nonce = mandate.get("nonce")
    if replay_cache is not None and isinstance(nonce, str):
        if nonce in replay_cache:
            return _deny("mandate_replayed", "mandate_replayed")
        replay_cache.add(nonce)

    return MandateVerificationResult(ok=True)


def _deny(code: str, message: str) -> MandateVerificationResult:
    return MandateVerificationResult(ok=False, error_code=code, message=message)


def _sign(payload: dict[str, Any], key: bytes | str) -> str:
    return hmac.new(_key_bytes(key), _canonical(payload), hashlib.sha256).hexdigest()


def _signature_valid(payload: dict[str, Any], key: bytes | str) -> bool:
    expected = payload.get("signature")
    if not isinstance(expected, str):
        return False
    unsigned = {k: v for k, v in payload.items() if k != "signature"}
    return hmac.compare_digest(expected, _sign(unsigned, key))


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode()


def _key_bytes(key: bytes | str) -> bytes:
    return key if isinstance(key, bytes) else key.encode()


def _format_dt(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _parse_dt(value: str) -> datetime | None:
    try:
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _check_constraints(
    constraints: dict[str, Any], params: dict[str, Any],
) -> str | None:
    for name, rules in constraints.items():
        if name not in params:
            return f"mandate_constraint_denied: missing {name}"
        value = params[name]
        if not isinstance(rules, dict):
            if value != rules:
                return f"mandate_constraint_denied: {name}"
            continue
        for op, expected in rules.items():
            if op == "eq" and value != expected:
                return f"mandate_constraint_denied: {name}"
            if op == "lte" and not value <= expected:
                return f"mandate_constraint_denied: {name}"
            if op == "lt" and not value < expected:
                return f"mandate_constraint_denied: {name}"
            if op == "gte" and not value >= expected:
                return f"mandate_constraint_denied: {name}"
            if op == "gt" and not value > expected:
                return f"mandate_constraint_denied: {name}"
            if op == "in" and value not in expected:
                return f"mandate_constraint_denied: {name}"
    return None
