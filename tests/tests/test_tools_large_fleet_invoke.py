# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Slow NATS-only large-fleet selector/invoke integration tests."""

import asyncio
import os
import time
import uuid

import pytest
from device_connect_edge.drivers import DeviceDriver, rpc
from device_connect_edge.types import DeviceIdentity, DeviceStatus

SETTLE_TIME = 0.3
DEFAULT_SCALE_FLEET_SIZE = 200
DISCOVERY_TIMEOUT = 60.0


def _scale_fleet_size(*, minimum: int = 1) -> int:
    return max(minimum, int(os.getenv("DC_SCALE_FLEET_SIZE", str(DEFAULT_SCALE_FLEET_SIZE))))


async def _wait_for_devices(messaging_url, expected_ids, timeout=DISCOVERY_TIMEOUT):
    """Connect and poll until all expected ``device_ids`` are visible."""
    from device_connect_agent_tools import connect
    from device_connect_agent_tools.connection import get_connection

    await asyncio.to_thread(connect, nats_url=messaging_url)
    deadline = time.monotonic() + timeout
    while True:
        conn = get_connection()
        devices = await asyncio.to_thread(conn.list_devices)
        ids = {d.get("device_id") for d in devices}
        if expected_ids.issubset(ids) or time.monotonic() > deadline:
            return devices
        await asyncio.sleep(0.25)


def _assert_compact_function_rows(rows):
    assert rows
    for row in rows:
        assert set(row) <= {"device_id", "name", "labels"}
        assert "parameters" not in row
        assert "description" not in row


def _assert_expanded_function_rows(rows):
    assert rows
    for row in rows:
        assert "device_id" in row
        assert "name" in row
        assert "parameters" in row
        assert "description" in row


class EmergencyStopDriver(DeviceDriver):
    """Dedicated test driver for the documented ``function(estop)`` pattern."""

    device_type = "test_estop_device"
    labels = {"category": "estop_target"}

    def __init__(self, location: str):
        super().__init__()
        self._location = location
        self.stopped = False

    @property
    def identity(self) -> DeviceIdentity:
        return DeviceIdentity(
            device_type="estop_target",
            manufacturer="TestCorp",
            model="TestStop-1000",
            firmware_version="1.0.0-test",
            arch="x86_64",
        )

    @property
    def status(self) -> DeviceStatus:
        return DeviceStatus(location=self._location, availability="available")

    @rpc(labels={"direction": "write", "safety": "critical"})
    async def estop(self, reason: str = "operator_request") -> dict:
        """Emergency-stop alias used by fleet-wide safety selectors."""
        self.stopped = True
        return {
            "status": "stopped",
            "reason": reason,
            "device_id": getattr(self, "_device_id", "unknown"),
        }

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass


async def _spawn_estop_fleet(
    device_spawner,
    prefix: str,
    count: int,
    *,
    location: str,
    registration_timeout: float = 45.0,
):
    spawned = await asyncio.gather(*[
        device_spawner._spawn(
            EmergencyStopDriver(location),
            f"{prefix}-{i:04d}",
            wait_for_registration=False,
        )
        for i in range(count)
    ])
    await asyncio.gather(*[
        device_spawner._wait_for_registration(device, registration_timeout)
        for device, _ in spawned
    ])
    return spawned


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.timeout(240)
@pytest.mark.parametrize("messaging_backend", ["nats"], indirect=True)
async def test_large_function_set_stays_compact_and_supports_drill_down(
    messaging_backend, messaging_url, clear_registry, device_spawner
):
    """Function discovery compacts broad results but expands narrow drill-downs."""
    if messaging_backend != "nats":
        pytest.skip("large-fleet selector test uses registry-backed NATS discovery")

    from device_connect_agent_tools import disconnect, discover
    from device_connect_agent_tools.tools import DC_FUNCTION_THRESHOLD

    total_size = _scale_fleet_size(minimum=(DC_FUNCTION_THRESHOLD // 3) + 3)
    sensor_count = total_size - 2
    prefix = f"itest-lfi-fn-{uuid.uuid4().hex[:8]}"
    sensor_location = f"{prefix}-sensor-room"
    camera_location = f"{prefix}-camera-room"
    sensor_ids = {f"{prefix}-sensor-{i:04d}" for i in range(sensor_count)}
    camera_ids = {f"{prefix}-cam-{i:04d}" for i in range(2)}

    await device_spawner.spawn_sensor_fleet(
        f"{prefix}-sensor",
        sensor_count,
        location=sensor_location,
        initial_temp=20.0,
        registration_timeout=45.0,
    )
    await asyncio.gather(*[
        device_spawner.spawn_camera(device_id, location=camera_location)
        for device_id in camera_ids
    ])
    await asyncio.sleep(SETTLE_TIME)

    await _wait_for_devices(messaging_url, sensor_ids | camera_ids)
    try:
        broad = await asyncio.to_thread(
            discover, f"device(location:{sensor_location}).function(*)", 0, 50,
        )
        assert broad["scope"] == "device_function"
        assert broad["matched"] == sensor_count * 3
        expected_returned = min(50, sensor_count * 3)
        assert broad["returned"] == expected_returned
        if sensor_count * 3 > 50:
            assert broad["next_offset"] == 50
        _assert_compact_function_rows(broad["results"])
        assert broad["label_histogram"]["direction"]["values"]["read"] == sensor_count
        assert broad["label_histogram"]["direction"]["values"]["write"] == sensor_count * 2

        sensor_drill_down = await asyncio.to_thread(
            discover, f"device({prefix}-sensor-0000).function(set_location)",
        )
        assert sensor_drill_down["matched"] == 1
        _assert_expanded_function_rows(sensor_drill_down["results"])
        assert sensor_drill_down["results"][0]["name"] == "set_location"
        assert "location" in sensor_drill_down["results"][0]["parameters"]["properties"]

        camera_drill_down = await asyncio.to_thread(
            discover, f"device(location:{camera_location}).function(capture_image)",
        )
        assert camera_drill_down["matched"] == 2
        _assert_expanded_function_rows(camera_drill_down["results"])
        assert {row["name"] for row in camera_drill_down["results"]} == {"capture_image"}
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.timeout(300)
@pytest.mark.parametrize("messaging_backend", ["nats"], indirect=True)
async def test_invoke_many_estop_alias_targets_only_estop_functions_at_scale(
    messaging_backend, messaging_url, clear_registry, device_spawner
):
    """``function(estop)`` fans out only to devices exposing the ESTOP alias."""
    if messaging_backend != "nats":
        pytest.skip("large-fleet ESTOP test uses registry-backed NATS discovery")

    from device_connect_agent_tools.tools import DC_FUNCTION_THRESHOLD

    estop_count = _scale_fleet_size(minimum=DC_FUNCTION_THRESHOLD + 1)
    decoy_count = 3
    prefix = f"itest-lfi-estop-{uuid.uuid4().hex[:8]}"
    location = f"{prefix}-room"
    estop_prefix = f"{prefix}-target"
    decoy_prefix = f"{prefix}-sensor"
    estop_ids = {f"{estop_prefix}-{i:04d}" for i in range(estop_count)}
    decoy_ids = {f"{decoy_prefix}-{i:04d}" for i in range(decoy_count)}

    await _spawn_estop_fleet(
        device_spawner,
        estop_prefix,
        estop_count,
        location=location,
    )
    await device_spawner.spawn_sensor_fleet(
        decoy_prefix,
        decoy_count,
        location=location,
        registration_timeout=45.0,
    )
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover, invoke_many

    await _wait_for_devices(messaging_url, estop_ids | decoy_ids)
    try:
        discovered = await asyncio.to_thread(discover, "function(estop)", 0, 50)
        assert discovered["scope"] == "function_only"
        assert discovered["matched"] == estop_count
        assert discovered["returned"] == min(50, estop_count)
        if estop_count > 50:
            assert discovered["next_offset"] == 50
        assert discovered["label_histogram"]["direction"]["values"]["write"] == (
            estop_count
        )
        assert discovered["label_histogram"]["safety"]["values"]["critical"] == (
            estop_count
        )
        assert not any(row["device_id"] in decoy_ids for row in discovered["results"])
        _assert_compact_function_rows(discovered["results"])

        result = await asyncio.to_thread(
            invoke_many,
            "function(estop)",
            {"reason": "release-qa"},
            10.0,
            64,
            "Large-fleet ESTOP alias release QA test",
        )
        assert result["candidates"] == estop_count
        assert result["matched"] == estop_count
        assert result["succeeded"] == estop_count
        assert result["failed"] == 0
        assert {row["device_id"] for row in result["results"]} == estop_ids
        assert {row["function"] for row in result["results"]} == {"estop"}
        assert all(row["result"]["status"] == "stopped" for row in result["results"])
        assert all(row["result"]["reason"] == "release-qa" for row in result["results"])
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.timeout(240)
@pytest.mark.parametrize("messaging_backend", ["nats"], indirect=True)
async def test_invoke_ambiguity_stays_bounded_in_large_fleet(
    messaging_backend, messaging_url, clear_registry, device_spawner
):
    """A large ambiguous invoke reports a capped candidate preview."""
    if messaging_backend != "nats":
        pytest.skip("large-fleet invoke test uses registry-backed NATS discovery")

    fleet_size = _scale_fleet_size(minimum=11)
    prefix = f"itest-lfi-amb-{uuid.uuid4().hex[:8]}"
    location = f"{prefix}-room"
    expected_ids = {f"{prefix}-{i:04d}" for i in range(fleet_size)}

    await device_spawner.spawn_sensor_fleet(
        prefix,
        fleet_size,
        location=location,
        initial_temp=22.0,
        registration_timeout=45.0,
    )
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, invoke

    await _wait_for_devices(messaging_url, expected_ids)
    try:
        result = await asyncio.to_thread(
            invoke,
            f"device(location:{location}).function(get_reading)",
            {"unit": "celsius"},
            "Large-fleet ambiguity bound test",
        )
        assert result["success"] is False
        assert result["error"]["code"] == "ambiguous_match"
        assert f"matched {fleet_size} functions" in result["error"]["message"]
        assert len(result["candidates"]) == 10
        assert all(set(row) == {"device_id", "function"} for row in result["candidates"])
        assert {row["function"] for row in result["candidates"]} == {"get_reading"}
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.timeout(300)
@pytest.mark.parametrize("messaging_backend", ["nats"], indirect=True)
async def test_invoke_many_partial_failure_accounting_at_scale(
    messaging_backend, messaging_url, clear_registry, device_spawner
):
    """Large fan-out keeps deterministic failures separate from successes."""
    if messaging_backend != "nats":
        pytest.skip("large-fleet invoke_many test uses registry-backed NATS discovery")

    fleet_size = _scale_fleet_size(minimum=12)
    failing_count = max(2, min(10, fleet_size // 10))
    healthy_count = fleet_size - failing_count
    prefix = f"itest-lfi-pf-{uuid.uuid4().hex[:8]}"
    location = f"{prefix}-room"
    healthy_prefix = f"{prefix}-ok"
    failing_prefix = f"{prefix}-fail"
    healthy_ids = {f"{healthy_prefix}-{i:04d}" for i in range(healthy_count)}
    failing_ids = {f"{failing_prefix}-{i:04d}" for i in range(failing_count)}

    await device_spawner.spawn_sensor_fleet(
        healthy_prefix,
        healthy_count,
        location=location,
        initial_temp=21.0,
        registration_timeout=45.0,
    )
    await device_spawner.spawn_sensor_fleet(
        failing_prefix,
        failing_count,
        failure_rate=1.0,
        location=location,
        initial_temp=21.0,
        registration_timeout=45.0,
    )
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, invoke_many

    await _wait_for_devices(messaging_url, healthy_ids | failing_ids)
    try:
        result = await asyncio.to_thread(
            invoke_many,
            f"device(location:{location}).function(get_reading)",
            {"unit": "celsius"},
            10.0,
            64,
            "Large-fleet partial failure accounting test",
        )
        assert result["candidates"] == fleet_size
        assert result["matched"] == fleet_size
        assert result["succeeded"] == healthy_count
        assert result["failed"] == failing_count
        assert result["succeeded"] + result["failed"] == result["candidates"]
        assert {row["device_id"] for row in result["results"]} == healthy_ids
        assert {row["device_id"] for row in result["errors"]} == failing_ids
        for row in result["errors"]:
            assert row["function"] == "get_reading"
            assert row["error"]["code"]
            assert row["error"]["message"]
    finally:
        await asyncio.to_thread(disconnect)
