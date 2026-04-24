# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for device-connect-agent-tools invoke_device().

Tests that the agent SDK can invoke device RPCs via the messaging backend.
"""

import asyncio
import pytest


SETTLE_TIME = 0.3


@pytest.mark.asyncio
@pytest.mark.integration
async def test_invoke_sensor_reading(device_spawner, messaging_url):
    """invoke_device() should call sensor's get_reading and return result."""
    await device_spawner.spawn_sensor(
        "itest-tools-invoke-sensor", initial_temp=23.5, initial_humidity=50.0,
    )
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, invoke_device

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        result = await asyncio.to_thread(
            invoke_device,
            device_id="itest-tools-invoke-sensor",
            function="get_reading",
            params={"unit": "celsius"},
            llm_reasoning="Testing sensor read",
        )
        assert isinstance(result, dict)
        assert result.get("success") is True or "temperature" in result.get("result", {})
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_invoke_robot_dispatch(device_spawner, event_capture, messaging_url):
    """invoke_device() should dispatch robot and trigger cleaning."""
    await device_spawner.spawn_robot(
        "itest-tools-invoke-robot", clean_duration=0.3,
    )
    await asyncio.sleep(SETTLE_TIME)

    async with event_capture.subscribe("device-connect.*.itest-tools-invoke-robot.event.*") as events:
        from device_connect_agent_tools import connect, disconnect, invoke_device

        await asyncio.to_thread(connect, nats_url=messaging_url)
        try:
            result = await asyncio.to_thread(
                invoke_device,
                device_id="itest-tools-invoke-robot",
                function="dispatch_robot",
                params={"zone_id": "zone-tools"},
                llm_reasoning="Testing robot dispatch via tools",
            )
            assert isinstance(result, dict)
        finally:
            await asyncio.to_thread(disconnect)

        event = await events.wait_for("cleaning_finished", timeout=10)
        assert event.data["zone_id"] == "zone-tools"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_invoke_unknown_device(messaging_url):
    """invoke_device() on non-existent device should return error."""
    from device_connect_agent_tools import connect, disconnect, invoke_device

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        result = await asyncio.to_thread(
            invoke_device,
            device_id="nonexistent-device-xyz",
            function="ping",
            llm_reasoning="Testing error handling",
        )
        assert isinstance(result, dict)
        assert result.get("success") is False
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_invoke_camera_capture(device_spawner, messaging_url):
    """invoke_device() should capture image from camera."""
    await device_spawner.spawn_camera("itest-tools-invoke-cam")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, invoke_device

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        result = await asyncio.to_thread(
            invoke_device,
            device_id="itest-tools-invoke-cam",
            function="capture_image",
            params={"resolution": "720p"},
            llm_reasoning="Testing camera capture via tools",
        )
        assert isinstance(result, dict)
    finally:
        await asyncio.to_thread(disconnect)
