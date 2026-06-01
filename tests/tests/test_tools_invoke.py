# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for selector-driven invocation tools.

Covers ``invoke()`` and ``invoke_many()`` against real devices registered
via the messaging backend. Exercises single-match, ambiguous-match,
selector-scope rejection, parallel fan-out, and partial-failure semantics
end-to-end.
"""

import asyncio
import time
import uuid

import pytest

from fixtures.scale import scale_fleet_size, spawn_scale_sensor_fleet

SETTLE_TIME = 0.3
DISCOVERY_TIMEOUT = 5.0


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


# -- invoke ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_invoke_sensor_reading(device_spawner, messaging_url):
    """invoke() calls sensor.get_reading and returns the reading payload."""
    await device_spawner.spawn_sensor(
        "itest-inv-read-sensor", initial_temp=23.5, initial_humidity=50.0,
    )
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, invoke

    await _wait_for_devices(messaging_url, {"itest-inv-read-sensor"})
    try:
        result = await asyncio.to_thread(
            invoke,
            "device(itest-inv-read-sensor).function(get_reading)",
            {"unit": "celsius"},
            "Testing sensor read",
        )
        assert result["success"] is True
        assert result["device_id"] == "itest-inv-read-sensor"
        assert result["function"] == "get_reading"
        assert "temperature" in result["result"]
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.timeout(180)
@pytest.mark.parametrize("messaging_backend", ["nats"], indirect=True)
async def test_scalable_fleet_discovery_and_invoke_many(
    messaging_backend, messaging_url, clear_registry, device_spawner
):
    """Discover and invoke hundreds of Docker-backed simulated devices."""
    if messaging_backend != "nats":
        pytest.skip("scale test uses registry-backed NATS discovery")

    fleet_size = scale_fleet_size()
    prefix = f"itest-scale-{uuid.uuid4().hex[:8]}"
    location = f"{prefix}-room"
    expected_ids = await spawn_scale_sensor_fleet(
        device_spawner,
        prefix=prefix,
        fleet_size=fleet_size,
        settle_time=SETTLE_TIME,
        location=location,
        registration_timeout=30.0,
    )

    from device_connect_agent_tools import (
        disconnect,
        discover,
        discover_labels,
        invoke_many,
    )

    devices = await _wait_for_devices(
        messaging_url, expected_ids, timeout=45.0,
    )
    visible_ids = {d.get("device_id") for d in devices}
    assert expected_ids <= visible_ids

    try:
        labels = await asyncio.to_thread(discover_labels, "device.location")
        assert labels["axis"] == "device"
        assert labels["key"] == "location"
        assert labels["values"][location] == fleet_size

        page_size = 50 if fleet_size >= 100 else max(1, fleet_size // 2)

        first_page = await asyncio.to_thread(
            discover, f"device(location:{location})", 0, page_size,
        )
        assert first_page["scope"] == "device_only"
        assert first_page["matched"] == fleet_size
        assert first_page["returned"] == page_size
        assert first_page["next_offset"] == page_size

        second_page = await asyncio.to_thread(
            discover,
            f"device(location:{location})",
            first_page["next_offset"],
            page_size,
        )
        first_ids = {row["device_id"] for row in first_page["results"]}
        second_ids = {row["device_id"] for row in second_page["results"]}
        assert first_ids
        assert second_ids
        assert first_ids.isdisjoint(second_ids)

        functions = await asyncio.to_thread(
            discover, f"device(location:{location}).function(get_reading)", 0, 25,
        )
        assert functions["scope"] == "device_function"
        assert functions["matched"] == fleet_size
        assert functions["returned"] == min(25, fleet_size)

        result = await asyncio.to_thread(
            invoke_many,
            f"device(location:{location}).function(get_reading)",
            {"unit": "celsius"},
            10.0,
            64,
            "Scale integration test fan-out",
        )
        assert result["candidates"] == fleet_size
        assert result["matched"] == fleet_size
        assert result["succeeded"] == fleet_size
        assert result["failed"] == 0
        assert {row["device_id"] for row in result["results"]} == expected_ids
        assert all(row["result"]["unit"] == "celsius" for row in result["results"])
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_invoke_robot_dispatch(device_spawner, event_capture, messaging_url):
    """invoke() dispatches the robot and the cleaning_finished event arrives."""
    await device_spawner.spawn_robot(
        "itest-inv-robot", clean_duration=0.3,
    )
    await asyncio.sleep(SETTLE_TIME)

    async with event_capture.subscribe(
        "device-connect.*.itest-inv-robot.event.*"
    ) as events:
        from device_connect_agent_tools import disconnect, invoke

        await _wait_for_devices(messaging_url, {"itest-inv-robot"})
        try:
            result = await asyncio.to_thread(
                invoke,
                "device(itest-inv-robot).function(dispatch_robot)",
                {"zone_id": "zone-tools"},
                "Testing robot dispatch",
            )
            assert result["success"] is True
        finally:
            await asyncio.to_thread(disconnect)

        event = await events.wait_for("cleaning_finished", timeout=10)
        assert event.data["zone_id"] == "zone-tools"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_invoke_no_match_returns_no_match(device_spawner, messaging_url):
    """A selector that resolves to zero functions returns ``no_match``."""
    await device_spawner.spawn_camera("itest-inv-nomatch-cam", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, invoke

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        result = await asyncio.to_thread(
            invoke,
            "device(itest-inv-nomatch-cam).function(does_not_exist)",
        )
        assert result["success"] is False
        assert result["error"]["code"] == "no_match"
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_invoke_ambiguous_match_returns_error(device_spawner, messaging_url):
    """A selector matching multiple (device, function) tuples returns an error."""
    await device_spawner.spawn_camera("itest-inv-amb-cam-1", location="lab-A")
    await device_spawner.spawn_camera("itest-inv-amb-cam-2", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, invoke

    await _wait_for_devices(
        messaging_url, {"itest-inv-amb-cam-1", "itest-inv-amb-cam-2"}
    )
    try:
        result = await asyncio.to_thread(
            invoke, "device(itest-inv-amb-cam-*).function(capture_image)",
        )
        assert result["success"] is False
        assert result["error"]["code"] == "ambiguous_match"
        cand_ids = {c["device_id"] for c in result["candidates"]}
        assert {"itest-inv-amb-cam-1", "itest-inv-amb-cam-2"} <= cand_ids
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_invoke_device_only_scope_rejected(device_spawner, messaging_url):
    """A device-only selector cannot resolve to a function."""
    await device_spawner.spawn_camera("itest-inv-scope-cam", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, invoke

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        result = await asyncio.to_thread(invoke, "device(itest-inv-scope-cam)")
        assert result["success"] is False
        assert result["error"]["code"] == "invalid_invoke_scope"
    finally:
        await asyncio.to_thread(disconnect)


# -- invoke_many ----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_invoke_many_succeeds_across_devices(device_spawner, messaging_url):
    """invoke_many() fans out a single function across multiple matching devices."""
    await device_spawner.spawn_camera("itest-inv-many-cam-1", location="lab-A")
    await device_spawner.spawn_camera("itest-inv-many-cam-2", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, invoke_many

    await _wait_for_devices(
        messaging_url, {"itest-inv-many-cam-1", "itest-inv-many-cam-2"}
    )
    try:
        result = await asyncio.to_thread(
            invoke_many,
            "device(itest-inv-many-cam-*).function(capture_image)",
            {"resolution": "720p"},
        )
        assert result["candidates"] == 2
        assert result["matched"] == 2
        assert result["succeeded"] == 2
        assert result["failed"] == 0
        ids = {row["device_id"] for row in result["results"]}
        assert ids == {"itest-inv-many-cam-1", "itest-inv-many-cam-2"}
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_invoke_many_partial_failure(device_spawner, messaging_url):
    """A failing target is recorded in errors while siblings succeed."""
    await device_spawner.spawn_camera(
        "itest-inv-many-pf-cam-1", location="lab-A", failure_rate=1.0,
    )
    await device_spawner.spawn_camera(
        "itest-inv-many-pf-cam-2", location="lab-A",
    )
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, invoke_many

    await _wait_for_devices(
        messaging_url,
        {"itest-inv-many-pf-cam-1", "itest-inv-many-pf-cam-2"},
    )
    try:
        result = await asyncio.to_thread(
            invoke_many,
            "device(itest-inv-many-pf-cam-*).function(capture_image)",
        )
        assert result["candidates"] == 2
        assert result["matched"] == 2
        assert result["succeeded"] == 1
        assert result["failed"] == 1
        success_ids = {row["device_id"] for row in result["results"]}
        error_ids = {row["device_id"] for row in result["errors"]}
        assert success_ids == {"itest-inv-many-pf-cam-2"}
        assert error_ids == {"itest-inv-many-pf-cam-1"}
        for row in result["errors"]:
            assert "code" in row["error"]
            assert "message" in row["error"]
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_invoke_many_zero_candidates(device_spawner, messaging_url):
    """No matches yields an empty envelope, not an error."""
    await device_spawner.spawn_camera("itest-inv-many-zero-cam", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, invoke_many

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        result = await asyncio.to_thread(
            invoke_many,
            "device(itest-no-such-device).function(capture_image)",
        )
        assert result["candidates"] == 0
        assert result["matched"] == 0
        assert result["succeeded"] == 0
        assert result["failed"] == 0
        assert result["results"] == []
        assert result["errors"] == []
        assert "error" not in result
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_invoke_many_function_only_selector(device_spawner, messaging_url):
    """function(<name>) selects the function across the whole fleet."""
    await device_spawner.spawn_sensor(
        "itest-inv-many-fo-sensor", initial_temp=20.0,
    )
    await device_spawner.spawn_camera("itest-inv-many-fo-cam", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, invoke_many

    await _wait_for_devices(
        messaging_url, {"itest-inv-many-fo-cam", "itest-inv-many-fo-sensor"}
    )
    try:
        result = await asyncio.to_thread(invoke_many, "function(get_reading)")
        ids = {row["device_id"] for row in result["results"]}
        assert "itest-inv-many-fo-sensor" in ids
        # Camera does not have get_reading; should not be in results.
        assert "itest-inv-many-fo-cam" not in ids
    finally:
        await asyncio.to_thread(disconnect)
