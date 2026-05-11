# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for portal mandate and receipt helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from device_connect_edge import create_closed_mandate, create_open_mandate
from device_connect_server.portal.services import execution_receipts, mandates


PRINCIPAL_KEY = "principal-secret"
AGENT_KEY = "agent-secret"


DEVICE_DOC = {
    "device_id": "acme-lock-001",
    "capabilities": {
        "functions": [
            {
                "name": "unlock",
                "mandate": {"required": True, "scope": "actuation"},
            },
            {"name": "get_status"},
        ]
    },
}


def _mandate(params: dict | None = None) -> dict:
    now = datetime.now(timezone.utc)
    params = params or {"duration_s": 30}
    open_mandate = create_open_mandate(
        principal="operator",
        agent="agent-1",
        device_id="acme-lock-001",
        methods=["unlock"],
        constraints={"duration_s": {"lte": 60}},
        not_before=now - timedelta(seconds=5),
        not_after=now + timedelta(minutes=5),
        key=PRINCIPAL_KEY,
    )
    return create_closed_mandate(
        open_mandate=open_mandate,
        agent="agent-1",
        device_id="acme-lock-001",
        method="unlock",
        params=params,
        key=AGENT_KEY,
        issued_at=now,
    )


def test_get_function_mandate_policy():
    assert mandates.get_function_mandate_policy(DEVICE_DOC, "unlock") == {
        "required": True,
        "scope": "actuation",
    }
    assert mandates.get_function_mandate_policy(DEVICE_DOC, "get_status") is None


def test_extract_and_attach_mandate_preserves_existing_meta():
    mandate = _mandate()
    params = {"duration_s": 30, "_dc_meta": {"traceparent": "trace"}}

    assert mandates.extract_mandate({"mandate": mandate}, params) == mandate
    attached = mandates.attach_mandate(
        mandates.strip_dc_meta(params), params, mandate,
    )

    assert attached["duration_s"] == 30
    assert attached["_dc_meta"]["traceparent"] == "trace"
    assert attached["_dc_meta"]["mandate"] == mandate


def test_verify_server_mandate_validates_protected_function(monkeypatch):
    monkeypatch.setenv(
        "DC_MANDATE_KEYS_JSON",
        '{"operator":"principal-secret","agent-1":"agent-secret"}',
    )
    mandates._SERVER_MANDATE_REPLAY_CACHE.clear()

    result = mandates.verify_server_mandate(
        device_doc=DEVICE_DOC,
        device_id="acme-lock-001",
        function="unlock",
        params={"duration_s": 30},
        mandate=_mandate(),
    )

    assert result.ok is True


def test_verify_server_mandate_denies_missing_mandate(monkeypatch):
    monkeypatch.setenv(
        "DC_MANDATE_KEYS_JSON",
        '{"operator":"principal-secret","agent-1":"agent-secret"}',
    )

    result = mandates.verify_server_mandate(
        device_doc=DEVICE_DOC,
        device_id="acme-lock-001",
        function="unlock",
        params={"duration_s": 30},
        mandate=None,
    )

    assert result.ok is False
    assert result.error_code == "mandate_required"


def test_execution_receipt_hashes_payload_and_can_sign(monkeypatch):
    monkeypatch.setenv("DC_RECEIPT_SIGNING_KEY", "receipt-secret")

    receipt = execution_receipts.build_receipt(
        trace_id="trace-1",
        tenant="acme",
        actor={"token_id": "tok-1", "username": "alice"},
        device_id="acme-lock-001",
        function="unlock",
        params={"duration_s": 30},
        status="succeeded",
        elapsed_ms=12,
        response={"ok": True},
        mandate=_mandate(),
        mandate_required=True,
        mandate_verified=True,
    )

    assert receipt["receipt_id"].startswith("rcpt-")
    assert receipt["params_sha256"]
    assert receipt["response_sha256"]
    assert receipt["signature"]
    assert receipt["mandate"]["verified"] is True
