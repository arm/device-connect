# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for sensor device lifecycle.

Tests that a TestSensorDriver registers, is discoverable, and can
be invoked via device-connect-agent-tools.
"""

import asyncio
import pytest


SETTLE_TIME = 0.5


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sensor_registers(device_spawner, messaging_backend):
    """A sensor device should register and get a registration ID."""
    device, driver = await device_spawner.spawn_sensor("itest-sensor-reg")
    if messaging_backend == "zenoh":
        assert device._d2d_announcer is not None
    else:
        assert device._registration_id is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sensor_discoverable_via_tools(device_spawner, messaging_url):
    """A registered sensor should appear in device-connect-agent-tools discover results."""
    device, driver = await device_spawner.spawn_sensor("itest-sensor-disc")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, discover_devices

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        devices = await asyncio.to_thread(discover_devices)
        ids = [d["device_id"] for d in devices]
        assert "itest-sensor-disc" in ids
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sensor_get_reading(device_spawner, messaging_url):
    """Invoke the sensor's get_reading RPC and verify payload structure."""
    device, driver = await device_spawner.spawn_sensor("itest-sensor-read")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, invoke_device

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        result = await asyncio.to_thread(
            invoke_device,
            device_id="itest-sensor-read",
            function="get_reading",
            params={"unit": "celsius"},
            llm_reasoning="Testing sensor read",
        )
        assert result.get("success") is True
        reading = result["result"]
        assert "temperature" in reading
        assert "humidity" in reading
        assert reading["unit"] == "celsius"
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sensor_get_reading_fahrenheit(device_spawner, messaging_url):
    """Invoke get_reading with fahrenheit unit."""
    device, driver = await device_spawner.spawn_sensor("itest-sensor-fahr")
    driver.set_values(temp=100.0, humidity=50.0)
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, invoke_device

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        result = await asyncio.to_thread(
            invoke_device,
            device_id="itest-sensor-fahr",
            function="get_reading",
            params={"unit": "fahrenheit"},
            llm_reasoning="Testing fahrenheit conversion",
        )
        assert result.get("success") is True
        reading = result["result"]
        assert reading["unit"] == "fahrenheit"
        # 100C = 212F
        assert abs(reading["temperature"] - 212.0) < 0.1
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sensor_set_location(device_spawner, messaging_url):
    """Invoke the sensor's set_location RPC."""
    device, driver = await device_spawner.spawn_sensor("itest-sensor-loc")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, invoke_device

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        result = await asyncio.to_thread(
            invoke_device,
            device_id="itest-sensor-loc",
            function="set_location",
            params={"location": "warehouse-B"},
            llm_reasoning="Testing set_location",
        )
        assert result.get("success") is True
        assert result["result"]["status"] == "success"
        assert result["result"]["location"] == "warehouse-B"
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sensor_capabilities(device_spawner, messaging_url):
    """Sensor should report its functions and events."""
    device, driver = await device_spawner.spawn_sensor("itest-sensor-caps")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, discover_devices

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        devices = await asyncio.to_thread(discover_devices)
        sensor = next(d for d in devices if d["device_id"] == "itest-sensor-caps")
        # device-connect-agent-tools restructures capabilities: functions are dicts, events are strings
        func_names = [f["name"] for f in sensor.get("functions", [])]
        events = sensor.get("events", [])

        assert "get_reading" in func_names
        assert "set_threshold" in func_names
        assert "reading" in events
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_multiple_sensors(device_spawner, messaging_url):
    """Multiple sensors should coexist.

    Replaces the previous single fixed-sleep settle with a poll-until-both-
    visible loop. SETTLE_TIME=0.5s is too tight for Zenoh D2D peer
    convergence under CI load — both peers' presence announcements need to
    propagate before discover_devices() sees them, and which one arrives
    first is non-deterministic.
    """
    _, _ = await device_spawner.spawn_sensor("itest-sensor-a")
    _, _ = await device_spawner.spawn_sensor("itest-sensor-b")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, discover_devices

    expected = {"itest-sensor-a", "itest-sensor-b"}
    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        deadline = asyncio.get_event_loop().time() + 10.0
        ids: set[str] = set()
        while asyncio.get_event_loop().time() < deadline:
            devices = await asyncio.to_thread(discover_devices, refresh=True)
            ids = {d["device_id"] for d in devices}
            if expected <= ids:
                break
            await asyncio.sleep(0.5)
        assert "itest-sensor-a" in ids, f"sensor-a not in {ids}"
        assert "itest-sensor-b" in ids, f"sensor-b not in {ids}"
    finally:
        await asyncio.to_thread(disconnect)
