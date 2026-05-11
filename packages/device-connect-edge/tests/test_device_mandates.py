# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Runtime enforcement tests for mandate-protected RPCs."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from device_connect_edge.device import DeviceRuntime
from device_connect_edge.drivers import DeviceDriver, requires_mandate, rpc
from device_connect_edge.mandates import create_closed_mandate, create_open_mandate


PRINCIPAL_KEY = b"principal-secret"
AGENT_KEY = b"agent-secret"


class LockDriver(DeviceDriver):
    device_type = "lock"

    def __init__(self):
        super().__init__()
        self.unlock_calls = 0

    @requires_mandate(scope="actuation")
    @rpc(labels={"direction": "write", "safety": "critical"})
    async def unlock(self, duration_s: int) -> dict:
        """Unlock for a bounded duration."""
        self.unlock_calls += 1
        return {"unlocked": True, "duration_s": duration_s}

    @rpc(labels={"direction": "read"})
    async def get_status(self) -> dict:
        """Return lock status."""
        return {"locked": True}


def _valid_mandate(params: dict | None = None) -> dict:
    now = datetime.now(timezone.utc)
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


def _runtime(driver: LockDriver) -> DeviceRuntime:
    return DeviceRuntime(
        driver=driver,
        device_id="lock-001",
        messaging_urls=["nats://localhost:4222"],
        mandate_keys={"operator": PRINCIPAL_KEY, "agent-1": AGENT_KEY},
    )


async def _invoke_callback(rt: DeviceRuntime, method: str, params: dict) -> dict:
    rt.messaging = AsyncMock()
    rt.messaging.subscribe = AsyncMock()
    await rt._cmd_subscription()
    on_msg = rt.messaging.subscribe.call_args[1]["callback"]
    await on_msg(
        json.dumps({
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": method,
            "params": params,
        }).encode(),
        reply_subject="reply.inbox.1",
    )
    return json.loads(rt.messaging.publish.call_args[0][1])


class TestRequiresMandateDecorator:
    def test_capability_metadata_includes_mandate_requirement(self):
        driver = LockDriver()
        fn = next(f for f in driver.functions if f.name == "unlock")
        assert fn.mandate == {"required": True, "scope": "actuation"}

    def test_unprotected_capability_has_no_mandate_requirement(self):
        driver = LockDriver()
        fn = next(f for f in driver.functions if f.name == "get_status")
        assert fn.mandate is None


class TestCommandMandateEnforcement:
    @pytest.mark.asyncio
    async def test_protected_rpc_without_mandate_is_denied_before_driver_call(self):
        driver = LockDriver()
        response = await _invoke_callback(
            _runtime(driver), "unlock", {"duration_s": 30},
        )

        assert response["error"]["code"] == -32041
        assert "mandate_required" in response["error"]["message"]
        assert driver.unlock_calls == 0

    @pytest.mark.asyncio
    async def test_protected_rpc_with_valid_mandate_executes(self):
        driver = LockDriver()
        response = await _invoke_callback(
            _runtime(driver),
            "unlock",
            {"duration_s": 30, "_dc_meta": {"mandate": _valid_mandate()}},
        )

        assert response["result"] == {"unlocked": True, "duration_s": 30}
        assert driver.unlock_calls == 1

    @pytest.mark.asyncio
    async def test_unprotected_rpc_executes_without_mandate(self):
        response = await _invoke_callback(_runtime(LockDriver()), "get_status", {})
        assert response["result"] == {"locked": True}

    @pytest.mark.asyncio
    async def test_broadcast_protected_rpc_without_mandate_is_denied(self):
        driver = LockDriver()
        rt = _runtime(driver)
        rt.messaging = AsyncMock()

        await rt._handle_broadcast_envelope(
            {
                "correlation_id": "br-1",
                "function": "unlock",
                "params": {"duration_s": 30},
            },
            "br-1",
        )

        payload = json.loads(rt.messaging.publish.call_args[0][1])
        assert payload["success"] is False
        assert payload["error"]["code"] == "mandate_required"
        assert driver.unlock_calls == 0

    @pytest.mark.asyncio
    async def test_broadcast_protected_rpc_with_valid_mandate_executes(self):
        driver = LockDriver()
        rt = _runtime(driver)
        rt.messaging = AsyncMock()

        await rt._handle_broadcast_envelope(
            {
                "correlation_id": "br-1",
                "function": "unlock",
                "params": {
                    "duration_s": 30,
                    "_dc_meta": {"mandate": _valid_mandate()},
                },
            },
            "br-1",
        )

        payload = json.loads(rt.messaging.publish.call_args[0][1])
        assert payload["success"] is True
        assert payload["result"] == {"unlocked": True, "duration_s": 30}
        assert driver.unlock_calls == 1
