# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Server-side helpers for Device Mandates."""

from __future__ import annotations

import json
import os
from typing import Any

from device_connect_edge.mandates import (
    MandateInvocationContext,
    MandateVerificationResult,
    verify_mandate,
)


_SERVER_MANDATE_REPLAY_CACHE: set[str] = set()


def get_function_mandate_policy(
    device_doc: dict[str, Any] | None,
    function: str,
) -> dict[str, Any] | None:
    """Return mandate policy metadata for a function in a registry document."""
    capabilities = (device_doc or {}).get("capabilities") or {}
    for fn in capabilities.get("functions") or []:
        if fn.get("name") == function:
            mandate = fn.get("mandate")
            return mandate if isinstance(mandate, dict) else None
    return None


def extract_mandate(
    body: dict[str, Any],
    params: dict[str, Any],
) -> dict[str, Any] | None:
    """Extract mandate from top-level body or params._dc_meta."""
    mandate = body.get("mandate")
    if isinstance(mandate, dict):
        return mandate
    dc_meta = params.get("_dc_meta")
    if isinstance(dc_meta, dict) and isinstance(dc_meta.get("mandate"), dict):
        return dc_meta["mandate"]
    return None


def strip_dc_meta(params: dict[str, Any]) -> dict[str, Any]:
    """Return user parameters only, excluding reserved Device Connect metadata."""
    return {k: v for k, v in params.items() if k != "_dc_meta"}


def attach_mandate(
    params: dict[str, Any],
    source_params: dict[str, Any],
    mandate: dict[str, Any] | None,
) -> dict[str, Any]:
    """Attach a mandate to params._dc_meta while preserving existing metadata."""
    out = dict(params)
    existing_meta = source_params.get("_dc_meta")
    meta = dict(existing_meta) if isinstance(existing_meta, dict) else {}
    if mandate is not None:
        meta["mandate"] = mandate
    if meta:
        out["_dc_meta"] = meta
    return out


def verify_server_mandate(
    *,
    device_doc: dict[str, Any] | None,
    device_id: str,
    function: str,
    params: dict[str, Any],
    mandate: dict[str, Any] | None,
) -> MandateVerificationResult:
    """Verify a mandate when policy requires it or a caller supplied one."""
    policy = get_function_mandate_policy(device_doc, function)
    mandate_required = bool(policy and policy.get("required"))
    if not mandate_required and mandate is None:
        return MandateVerificationResult(ok=True)
    return verify_mandate(
        mandate,
        context=MandateInvocationContext(
            device_id=device_id,
            method=function,
            params=params,
        ),
        key_resolver=resolve_mandate_key,
        replay_cache=_SERVER_MANDATE_REPLAY_CACHE,
    )


def resolve_mandate_key(principal_or_agent: str) -> bytes | str | None:
    """Resolve principal/agent signing keys from DC_MANDATE_KEYS_JSON."""
    raw = os.getenv("DC_MANDATE_KEYS_JSON", "")
    if not raw:
        return None
    try:
        keys = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(keys, dict):
        return None
    key = keys.get(principal_or_agent)
    return key if isinstance(key, str) else None
