# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for Device Mandate signing and verification helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from device_connect_edge.mandates import (
    MandateInvocationContext,
    create_closed_mandate,
    create_open_mandate,
    verify_mandate,
)


PRINCIPAL_KEY = b"principal-secret"
AGENT_KEY = b"agent-secret"


def _keys(principal: str) -> bytes | None:
    return {
        "operator": PRINCIPAL_KEY,
        "agent-1": AGENT_KEY,
    }.get(principal)


def _valid_mandate(params: dict | None = None) -> dict:
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    params = params or {"duration_s": 30}
    open_mandate = create_open_mandate(
        principal="operator",
        agent="agent-1",
        device_id="lock-001",
        methods=["unlock"],
        constraints={"duration_s": {"lte": 60}},
        not_before=now - timedelta(minutes=5),
        not_after=now + timedelta(minutes=5),
        key=PRINCIPAL_KEY,
        mandate_id="open-1",
    )
    return create_closed_mandate(
        open_mandate=open_mandate,
        agent="agent-1",
        device_id="lock-001",
        method="unlock",
        params=params,
        key=AGENT_KEY,
        issued_at=now,
        mandate_id="closed-1",
        nonce="nonce-1",
    )


def _context(**overrides) -> MandateInvocationContext:
    base = {
        "device_id": "lock-001",
        "method": "unlock",
        "params": {"duration_s": 30},
        "now": datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return MandateInvocationContext(**base)


def test_valid_closed_mandate_verifies():
    result = verify_mandate(_valid_mandate(), context=_context(), key_resolver=_keys)
    assert result.ok is True
    assert result.error_code is None


def test_missing_mandate_fails_closed():
    result = verify_mandate(None, context=_context(), key_resolver=_keys)
    assert result.ok is False
    assert result.error_code == "mandate_required"


def test_tampered_parameters_fail_signature_check():
    mandate = _valid_mandate()
    mandate["invocation"]["params"]["duration_s"] = 45

    result = verify_mandate(mandate, context=_context(), key_resolver=_keys)

    assert result.ok is False
    assert result.error_code == "invalid_mandate_signature"


def test_wrong_device_is_denied():
    result = verify_mandate(
        _valid_mandate(),
        context=_context(device_id="other-lock"),
        key_resolver=_keys,
    )
    assert result.ok is False
    assert result.error_code == "mandate_device_denied"


def test_wrong_method_is_denied():
    result = verify_mandate(
        _valid_mandate(),
        context=_context(method="lock"),
        key_resolver=_keys,
    )
    assert result.ok is False
    assert result.error_code == "mandate_method_denied"


def test_expired_mandate_is_denied():
    result = verify_mandate(
        _valid_mandate(),
        context=_context(now=datetime(2026, 5, 11, 12, 10, tzinfo=timezone.utc)),
        key_resolver=_keys,
    )
    assert result.ok is False
    assert result.error_code == "mandate_expired"


def test_parameter_constraint_is_enforced():
    mandate = _valid_mandate(params={"duration_s": 75})
    result = verify_mandate(
        mandate,
        context=_context(params={"duration_s": 75}),
        key_resolver=_keys,
    )
    assert result.ok is False
    assert result.error_code == "mandate_constraint_denied"


def test_replay_cache_denies_reused_nonce():
    seen: set[str] = set()
    mandate = _valid_mandate()

    first = verify_mandate(
        mandate, context=_context(), key_resolver=_keys, replay_cache=seen,
    )
    second = verify_mandate(
        mandate, context=_context(), key_resolver=_keys, replay_cache=seen,
    )

    assert first.ok is True
    assert second.ok is False
    assert second.error_code == "mandate_replayed"
