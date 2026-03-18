"""Integration tests for multi-device scenarios.

End-to-end tests with 3+ devices and orchestrator coordination.
"""

import asyncio
import pytest


SETTLE_TIME = 0.1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_camera_robot_sensor_workflow(device_spawner, event_capture, mock_orchestrator):
    """Full workflow: camera detects → robot dispatches → sensor reads.

    1. Camera emits state_change_detected
    2. MockOrchestrator routes to robot.dispatch_robot
    3. Robot emits cleaning_finished
    4. MockOrchestrator routes cleaning_finished to sensor.get_reading
    """
    cam, cam_driver = await device_spawner.spawn_camera("itest-multi-cam")
    robot, robot_driver = await device_spawner.spawn_robot(
        "itest-multi-robot", clean_duration=0.3,
    )
    sensor, sensor_driver = await device_spawner.spawn_sensor(
        "itest-multi-sensor", initial_temp=25.0,
    )

    # Rule 1: Camera state_change → Robot dispatch
    mock_orchestrator.add_rule(
        on_event="state_change_detected",
        call_function="dispatch_robot",
        target_device_id="itest-multi-robot",
    )

    async with event_capture.subscribe("device-connect.*.*.event.*") as events:
        await asyncio.sleep(SETTLE_TIME)

        # Camera detects mess
        await cam_driver.trigger_event("state_change_detected", {
            "zone_id": "zone-multi",
            "class": "mess",
        })

        # Robot should finish cleaning
        event = await events.wait_for(
            event_name="cleaning_finished",
            device_id="itest-multi-robot",
            timeout=10,
        )
        assert event.data["zone_id"] == "zone-multi"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_two_cameras_one_robot(device_spawner, event_capture, mock_orchestrator):
    """Two cameras emit events, both routed to same robot."""
    cam1, cam1_driver = await device_spawner.spawn_camera("itest-multi-cam-1")
    cam2, cam2_driver = await device_spawner.spawn_camera("itest-multi-cam-2")
    robot, robot_driver = await device_spawner.spawn_robot(
        "itest-multi-shared-robot", clean_duration=0.3,
    )

    mock_orchestrator.add_rule(
        on_event="state_change_detected",
        call_function="dispatch_robot",
        target_device_id="itest-multi-shared-robot",
    )

    async with event_capture.subscribe("device-connect.*.itest-multi-shared-robot.event.*") as events:
        await asyncio.sleep(SETTLE_TIME)

        # First camera — robot should accept
        await cam1_driver.trigger_event("state_change_detected", {
            "zone_id": "zone-1", "class": "mess",
        })
        event = await events.wait_for("cleaning_finished", timeout=10)
        assert event.data["zone_id"] == "zone-1"

        # Second camera — robot should accept (done with first)
        await cam2_driver.trigger_event("state_change_detected", {
            "zone_id": "zone-2", "class": "mess",
        })
        event = await events.wait_for(
            "cleaning_finished",
            predicate=lambda e: e.data.get("zone_id") == "zone-2",
            timeout=10,
        )
        assert event.data["zone_id"] == "zone-2"
