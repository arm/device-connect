"""Integration tests for hierarchical discovery tools.

Tests describe_fleet(), list_devices(), get_device_functions() and the
small-fleet auto-expansion (SMALL_FLEET_THRESHOLD) against real devices
registered via the NATS registry.
"""

import asyncio
import time
from unittest.mock import patch

import pytest

SETTLE_TIME = 0.3
DISCOVERY_TIMEOUT = 5.0  # max seconds to wait for D2D peers to appear


# ── describe_fleet ──────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_describe_fleet_returns_summary(device_spawner, messaging_url):
    """describe_fleet() should return type/location counts."""
    await device_spawner.spawn_camera("itest-fleet-cam", location="lobby")
    await device_spawner.spawn_sensor("itest-fleet-sensor", location="lab")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect
    from device_connect_agent_tools.tools import describe_fleet

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        # D2D discovery is eventually consistent — poll until both peers visible
        deadline = time.monotonic() + DISCOVERY_TIMEOUT
        while True:
            result = await asyncio.to_thread(describe_fleet)
            if result["total_devices"] >= 2 or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.25)
        assert result["total_devices"] >= 2
        assert result["total_functions"] >= 1
        assert "by_type" in result
        assert "by_location" in result
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_describe_fleet_auto_expand_small(device_spawner, messaging_url):
    """Small fleet auto-includes full device details."""
    await device_spawner.spawn_camera("itest-fleet-expand-cam", location="lobby")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect
    from device_connect_agent_tools import tools as tools_mod

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        # With threshold high enough, devices should be auto-included
        with patch.object(tools_mod, "SMALL_FLEET_THRESHOLD", 100):
            result = await asyncio.to_thread(tools_mod.describe_fleet)
        assert "devices" in result, "Small fleet should auto-include devices"
        assert "hint" in result
        # Each device should have full function schemas
        cam = next(
            (d for d in result["devices"] if d["device_id"] == "itest-fleet-expand-cam"),
            None,
        )
        assert cam is not None
        assert len(cam["functions"]) >= 1
        assert "parameters" in cam["functions"][0]
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_describe_fleet_no_expand_when_disabled(device_spawner, messaging_url):
    """Threshold=0 disables auto-expansion."""
    await device_spawner.spawn_camera("itest-fleet-noexp-cam")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect
    from device_connect_agent_tools import tools as tools_mod

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        with patch.object(tools_mod, "SMALL_FLEET_THRESHOLD", 0):
            result = await asyncio.to_thread(tools_mod.describe_fleet)
        assert "devices" not in result
    finally:
        await asyncio.to_thread(disconnect)


# ── list_devices ────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_devices_compact(device_spawner, messaging_url):
    """list_devices() should return compact summaries with function_names."""
    await device_spawner.spawn_camera("itest-list-cam", location="lobby")
    await device_spawner.spawn_sensor("itest-list-sensor", location="lab")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect
    from device_connect_agent_tools.tools import list_devices

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        # D2D discovery is eventually consistent — poll until both peers visible
        deadline = time.monotonic() + DISCOVERY_TIMEOUT
        while True:
            result = await asyncio.to_thread(list_devices)
            if result["total"] >= 2 or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.25)
        assert result["total"] >= 2
        for d in result["devices"]:
            assert "device_id" in d
            assert "function_count" in d
            assert "function_names" in d
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_devices_filter_by_type(device_spawner, messaging_url):
    """list_devices(device_type=...) should filter results."""
    await device_spawner.spawn_camera("itest-listf-cam", location="lobby")
    await device_spawner.spawn_sensor("itest-listf-sensor", location="lab")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect
    from device_connect_agent_tools.tools import list_devices

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        deadline = time.monotonic() + DISCOVERY_TIMEOUT
        while True:
            result = await asyncio.to_thread(list_devices, device_type="camera")
            if result["total"] >= 1 or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.25)
        assert result["total"] >= 1
        for d in result["devices"]:
            assert "camera" in d["device_type"].lower()
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_devices_auto_expand_functions(device_spawner, messaging_url):
    """Small result set auto-includes function schemas."""
    await device_spawner.spawn_camera("itest-listexp-cam", location="lobby")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect
    from device_connect_agent_tools import tools as tools_mod

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        deadline = time.monotonic() + DISCOVERY_TIMEOUT
        while True:
            with patch.object(tools_mod, "SMALL_FLEET_THRESHOLD", 100):
                result = await asyncio.to_thread(tools_mod.list_devices)
            if result["total"] >= 1 or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.25)
        for d in result["devices"]:
            assert "functions" in d, "Small result set should auto-include functions"
            # Should still have compact fields too
            assert "function_names" in d
            assert "function_count" in d
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_devices_pagination(device_spawner, messaging_url):
    """list_devices() pagination with offset/limit."""
    await device_spawner.spawn_camera("itest-page-cam1")
    await device_spawner.spawn_camera("itest-page-cam2")
    await device_spawner.spawn_sensor("itest-page-sensor")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect
    from device_connect_agent_tools.tools import list_devices

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        # D2D discovery is eventually consistent — poll until all peers visible
        deadline = time.monotonic() + DISCOVERY_TIMEOUT
        while True:
            result = await asyncio.to_thread(list_devices, offset=0, limit=2)
            if result["total"] >= 3 or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.25)
        assert len(result["devices"]) <= 2
        assert result["total"] >= 3
        assert result["has_more"] is True
    finally:
        await asyncio.to_thread(disconnect)


# ── get_device_functions ────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_device_functions_returns_schemas(device_spawner, messaging_url):
    """get_device_functions() should return full schemas for one device."""
    await device_spawner.spawn_camera("itest-funcs-cam", location="lobby")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, get_connection
    from device_connect_agent_tools.tools import get_device_functions

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        deadline = time.monotonic() + DISCOVERY_TIMEOUT
        while True:
            conn = get_connection()
            if conn._provider and hasattr(conn._provider, "invalidate_cache"):
                conn._provider.invalidate_cache()
            result = await asyncio.to_thread(get_device_functions, "itest-funcs-cam")
            if "error" not in result or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.25)
        assert "error" not in result, f"Unexpected error: {result}"
        assert result["device_id"] == "itest-funcs-cam"
        assert len(result["functions"]) >= 1
        func = result["functions"][0]
        assert "name" in func
        assert "description" in func
        assert "parameters" in func
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_device_functions_not_found(messaging_url):
    """get_device_functions() for missing device returns error."""
    from device_connect_agent_tools import connect, disconnect
    from device_connect_agent_tools.tools import get_device_functions

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        result = await asyncio.to_thread(get_device_functions, "nonexistent-xyz")
        assert "error" in result
    finally:
        await asyncio.to_thread(disconnect)


# ── end-to-end drill-down ───────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_full_hierarchical_flow(device_spawner, messaging_url):
    """Full drill-down: describe_fleet → list_devices → get_device_functions → invoke_device."""
    await device_spawner.spawn_sensor(
        "itest-drill-sensor", location="lab", initial_temp=25.0,
    )
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, get_connection
    from device_connect_agent_tools.tools import (
        describe_fleet, list_devices, get_device_functions, invoke_device,
    )

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        conn = get_connection()

        # Step 1: fleet overview — poll until device is visible (D2D is eventually consistent)
        deadline = time.monotonic() + DISCOVERY_TIMEOUT
        while True:
            if conn._provider and hasattr(conn._provider, "invalidate_cache"):
                conn._provider.invalidate_cache()
            fleet = await asyncio.to_thread(describe_fleet)
            if fleet["total_devices"] >= 1 or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.25)
        assert fleet["total_devices"] >= 1

        # Step 2: list sensors
        listing = await asyncio.to_thread(list_devices, device_type="sensor")
        assert listing["total"] >= 1
        sensor_ids = [d["device_id"] for d in listing["devices"]]
        assert "itest-drill-sensor" in sensor_ids

        # Step 3: get function schemas
        funcs = await asyncio.to_thread(get_device_functions, "itest-drill-sensor")
        assert "error" not in funcs, f"Unexpected error: {funcs}"
        func_names = [f["name"] for f in funcs["functions"]]
        assert "get_reading" in func_names

        # Step 4: invoke
        result = await asyncio.to_thread(
            invoke_device,
            device_id="itest-drill-sensor",
            function="get_reading",
            params={"unit": "celsius"},
        )
        assert result.get("success") is True
    finally:
        await asyncio.to_thread(disconnect)


# ── regression tests ───────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_devices_includes_function_names(device_spawner, messaging_url):
    """list_devices() should return correct function_names from device capabilities."""
    await device_spawner.spawn_camera("itest-cap-cam", location="lobby")
    await device_spawner.spawn_sensor("itest-cap-sensor", location="lab", initial_temp=20.0)
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect
    from device_connect_agent_tools import tools as tools_mod
    from device_connect_agent_tools.tools import list_devices

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        deadline = time.monotonic() + DISCOVERY_TIMEOUT
        cam_result = None
        while True:
            with patch.object(tools_mod, "SMALL_FLEET_THRESHOLD", 100):
                cam_result = await asyncio.to_thread(list_devices, device_type="camera")
            if cam_result["total"] >= 1 or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.25)
        assert cam_result["total"] >= 1
        cam = cam_result["devices"][0]
        assert cam["function_count"] >= 1
        assert "capture_image" in cam["function_names"]

        with patch.object(tools_mod, "SMALL_FLEET_THRESHOLD", 100):
            sensor_result = await asyncio.to_thread(list_devices, device_type="sensor")
        assert sensor_result["total"] >= 1
        sensor = sensor_result["devices"][0]
        assert sensor["function_count"] >= 3
        assert "get_reading" in sensor["function_names"]
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_invoke_nonexistent_device_returns_error(messaging_url):
    """invoke_device() for a missing device should return an error, not crash."""
    from device_connect_agent_tools import connect, disconnect
    from device_connect_agent_tools.tools import invoke_device

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        result = await asyncio.to_thread(
            invoke_device,
            device_id="does-not-exist-xyz",
            function="foo",
        )
        assert result.get("success") is not True
        assert "error" in result or "not found" in str(result).lower()
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_devices_includes_status(device_spawner, messaging_url):
    """list_devices() should include a status field for each device."""
    await device_spawner.spawn_camera("itest-status-cam", location="lobby")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect
    from device_connect_agent_tools import tools as tools_mod
    from device_connect_agent_tools.tools import list_devices

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        deadline = time.monotonic() + DISCOVERY_TIMEOUT
        result = None
        while True:
            with patch.object(tools_mod, "SMALL_FLEET_THRESHOLD", 100):
                result = await asyncio.to_thread(list_devices)
            if result["total"] >= 1 or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.25)
        assert result["total"] >= 1
        for dev in result["devices"]:
            assert "status" in dev, f"Device {dev.get('device_id')} missing status field"
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_describe_fleet_function_count_matches_actual(device_spawner, messaging_url):
    """describe_fleet() total_functions should match sum of individual device function counts."""
    await device_spawner.spawn_camera("itest-fcount-cam", location="lobby")
    await device_spawner.spawn_sensor("itest-fcount-sensor", location="lab", initial_temp=20.0)
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, get_connection
    from device_connect_agent_tools import tools as tools_mod
    from device_connect_agent_tools.tools import describe_fleet, get_device_functions

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        conn = get_connection()
        if conn._provider and hasattr(conn._provider, "invalidate_cache"):
            conn._provider.invalidate_cache()

        deadline = time.monotonic() + DISCOVERY_TIMEOUT
        fleet = None
        while True:
            with patch.object(tools_mod, "SMALL_FLEET_THRESHOLD", 100):
                fleet = await asyncio.to_thread(describe_fleet)
            if fleet["total_devices"] >= 2 or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.25)
        assert fleet["total_devices"] >= 2
        assert "devices" in fleet, "Auto-expansion should include devices list"

        # Cross-check: sum of individual function counts == total_functions
        individual_total = 0
        for dev in fleet["devices"]:
            funcs = await asyncio.to_thread(get_device_functions, dev["device_id"])
            if "error" not in funcs:
                individual_total += len(funcs["functions"])
        assert individual_total == fleet["total_functions"]
    finally:
        await asyncio.to_thread(disconnect)
