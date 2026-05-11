#!/usr/bin/env python3
# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Local examples for Device Mandates.

Run from the repository root:
    PYTHONPATH=packages/device-connect-edge python packages/device-connect-edge/examples/device_mandates/mandate_examples.py

Focused tests:
    pytest packages/device-connect-edge/tests/test_mandate_verifier.py packages/device-connect-edge/tests/test_device_mandates.py packages/device-connect-agent-tools/tests/test_agent_mandates.py -q
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from device_connect_edge import create_closed_mandate, create_open_mandate
from device_connect_edge.drivers import DeviceDriver, requires_mandate, rpc
from device_connect_edge.mandates import MandateInvocationContext, verify_mandate


PRINCIPAL_KEY = b"principal-demo-key"
AGENT_KEY = b"agent-demo-key"


class SmartLockDriver(DeviceDriver):
    """Smart lock with mandate-protected actuation."""

    device_type = "smart_lock"

    @requires_mandate(scope="actuation")
    @rpc()
    async def unlock(self, duration_s: int = 10) -> dict[str, Any]:
        return {"state": "unlocked", "duration_s": duration_s}

    @rpc()
    async def get_status(self) -> dict[str, str]:
        return {"state": "locked"}


class HeaterDriver(DeviceDriver):
    """Heater with mandate-protected setpoint changes."""

    device_type = "heater"

    @rpc()
    async def get_temperature(self) -> dict[str, float]:
        return {"current_c": 20.5}

    @requires_mandate(scope="actuation")
    @rpc()
    async def set_temperature(self, target_c: float) -> dict[str, float]:
        return {"target_c": target_c}


def key_resolver(principal: str) -> bytes | None:
    return {"operator": PRINCIPAL_KEY, "agent-1": AGENT_KEY}.get(principal)


def closed_mandate(
    *,
    device_id: str,
    method: str,
    params: dict[str, Any],
    constraints: dict[str, Any],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    open_mandate = create_open_mandate(
        principal="operator",
        agent="agent-1",
        device_id=device_id,
        methods=[method],
        constraints=constraints,
        not_before=now - timedelta(seconds=5),
        not_after=now + timedelta(minutes=5),
        key=PRINCIPAL_KEY,
    )
    return create_closed_mandate(
        open_mandate=open_mandate,
        agent="agent-1",
        device_id=device_id,
        method=method,
        params=params,
        key=AGENT_KEY,
        issued_at=now,
    )


def verify_example(
    *,
    label: str,
    mandate: dict[str, Any] | None,
    device_id: str,
    method: str,
    params: dict[str, Any],
    replay_cache: set[str] | None = None,
) -> None:
    result = verify_mandate(
        mandate,
        context=MandateInvocationContext(
            device_id=device_id,
            method=method,
            params=params,
        ),
        key_resolver=key_resolver,
        replay_cache=replay_cache,
    )
    outcome = "allowed" if result.ok else f"denied ({result.error_code})"
    print(f"{label}: {outcome}")


async def main() -> None:
    lock = SmartLockDriver()
    heater = HeaterDriver()

    unlock_policy = getattr(lock.unlock, "_mandate", None)
    heater_policy = getattr(heater.set_temperature, "_mandate", None)
    print(f"smart-lock unlock mandate policy: {unlock_policy}")
    print(f"heater set_temperature mandate policy: {heater_policy}")

    valid_unlock_params = {"duration_s": 20}
    valid_unlock = closed_mandate(
        device_id="lock-front-door",
        method="unlock",
        params=valid_unlock_params,
        constraints={"duration_s": {"lte": 30}},
    )
    verify_example(
        label="valid smart-lock unlock",
        mandate=valid_unlock,
        device_id="lock-front-door",
        method="unlock",
        params=valid_unlock_params,
    )
    verify_example(
        label="invalid smart-lock duration",
        mandate=valid_unlock,
        device_id="lock-front-door",
        method="unlock",
        params={"duration_s": 60},
    )

    valid_heat_params = {"target_c": 21.5}
    valid_heat = closed_mandate(
        device_id="heater-living-room",
        method="set_temperature",
        params=valid_heat_params,
        constraints={"target_c": {"gte": 18, "lte": 23}},
    )
    replay_cache: set[str] = set()
    verify_example(
        label="valid heater setpoint",
        mandate=valid_heat,
        device_id="heater-living-room",
        method="set_temperature",
        params=valid_heat_params,
        replay_cache=replay_cache,
    )
    verify_example(
        label="invalid heater replay",
        mandate=valid_heat,
        device_id="heater-living-room",
        method="set_temperature",
        params=valid_heat_params,
        replay_cache=replay_cache,
    )


if __name__ == "__main__":
    asyncio.run(main())
