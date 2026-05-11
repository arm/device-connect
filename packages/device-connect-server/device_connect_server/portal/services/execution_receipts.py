# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Execution receipt helpers for mandate-aware invokes."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timezone
from typing import Any

_RECEIPTS: list[dict[str, Any]] = []
_MAX_RECEIPTS = 1000


def build_receipt(
    *,
    trace_id: str,
    tenant: str,
    actor: dict[str, Any],
    device_id: str,
    function: str,
    params: dict[str, Any],
    status: str,
    elapsed_ms: int,
    response: Any = None,
    error: dict[str, Any] | None = None,
    mandate: dict[str, Any] | None = None,
    mandate_required: bool = False,
    mandate_verified: bool = False,
    mandate_error_code: str | None = None,
) -> dict[str, Any]:
    receipt = {
        "receipt_id": "rcpt-" + secrets.token_hex(8),
        "trace_id": trace_id,
        "tenant": tenant,
        "actor": {
            "token_id": actor.get("token_id"),
            "username": actor.get("username"),
        },
        "device_id": device_id,
        "function": function,
        "status": status,
        "authorized": status != "denied",
        "mandate": _mandate_summary(
            mandate,
            required=mandate_required,
            verified=mandate_verified,
            error_code=mandate_error_code,
        ),
        "params_sha256": hash_json(params),
        "response_sha256": hash_json(response) if response is not None else None,
        "error": error,
        "elapsed_ms": elapsed_ms,
        "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    receipt["signature"] = sign_receipt(receipt)
    return receipt


def record_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    """Append a receipt to the process-local audit log."""
    _RECEIPTS.append(dict(receipt))
    if len(_RECEIPTS) > _MAX_RECEIPTS:
        del _RECEIPTS[: len(_RECEIPTS) - _MAX_RECEIPTS]
    return receipt


def get_receipt(receipt_id: str) -> dict[str, Any] | None:
    for receipt in reversed(_RECEIPTS):
        if receipt.get("receipt_id") == receipt_id:
            return dict(receipt)
    return None


def list_receipts(
    *,
    tenant: str | None = None,
    device_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit or 100), 1000))
    out = []
    for receipt in reversed(_RECEIPTS):
        if tenant is not None and receipt.get("tenant") != tenant:
            continue
        if device_id is not None and receipt.get("device_id") != device_id:
            continue
        out.append(dict(receipt))
        if len(out) >= safe_limit:
            break
    return out


def hash_json(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        default=str,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def sign_receipt(receipt: dict[str, Any]) -> str | None:
    key = os.getenv("DC_RECEIPT_SIGNING_KEY")
    if not key:
        return None
    unsigned = {k: v for k, v in receipt.items() if k != "signature"}
    payload = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        default=str,
    ).encode()
    return hmac.new(key.encode(), payload, hashlib.sha256).hexdigest()


def _mandate_summary(
    mandate: dict[str, Any] | None,
    *,
    required: bool,
    verified: bool,
    error_code: str | None,
) -> dict[str, Any]:
    open_mandate = mandate.get("open_mandate") if isinstance(mandate, dict) else {}
    return {
        "required": required,
        "verified": verified,
        "id": mandate.get("id") if isinstance(mandate, dict) else None,
        "open_mandate_id": open_mandate.get("id") if isinstance(open_mandate, dict) else None,
        "principal": open_mandate.get("principal") if isinstance(open_mandate, dict) else None,
        "agent": mandate.get("agent") if isinstance(mandate, dict) else None,
        "error_code": error_code,
    }
