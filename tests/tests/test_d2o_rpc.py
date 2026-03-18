"""Integration tests for D2O (Device-to-Orchestrator) RPC routing.

Tests that MockOrchestrator can route events to device functions,
exercising the full NATS pub/sub + JSON-RPC path.
"""

import asyncio
import pytest


SETTLE_TIME = 0.1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_mock_orchestrator_routes_event_to_robot(device_spawner, event_capture, mock_orchestrator):
    """Camera event → MockOrchestrator → robot dispatch → cleaning_finished."""
    cam, cam_driver = await device_spawner.spawn_camera("itest-d2o-cam")
    robot, robot_driver = await device_spawner.spawn_robot("itest-d2o-robot", clean_duration=0.3)

    mock_orchestrator.add_rule(
        on_event="state_change_detected",
        call_function="dispatch_robot",
        target_device_id="itest-d2o-robot",
    )

    async with event_capture.subscribe("device-connect.*.*.event.*") as events:
        await asyncio.sleep(SETTLE_TIME)

        await cam_driver.trigger_event("state_change_detected", {
            "zone_id": "zone-A",
            "class": "mess",
        })

        event = await events.wait_for(
            event_name="cleaning_finished",
            device_id="itest-d2o-robot",
            timeout=10,
        )
        assert event.data["zone_id"] == "zone-A"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_mock_orchestrator_no_matching_rule(device_spawner, event_capture, mock_orchestrator):
    """Events without matching rules should be ignored."""
    mock_orchestrator.add_rule(
        on_event="motion_detected",
        call_function="dispatch_robot",
        target_device_type="cleaner_robot",
    )

    cam, cam_driver = await device_spawner.spawn_camera("itest-d2o-nomatch-cam")
    robot, _ = await device_spawner.spawn_robot("itest-d2o-nomatch-robot", clean_duration=0.3)

    async with event_capture.subscribe("device-connect.*.itest-d2o-nomatch-robot.event.*") as events:
        await asyncio.sleep(SETTLE_TIME)

        # Emit non-matching event
        await cam_driver.trigger_event("state_change_detected", {"zone_id": "zone-A"})

        with pytest.raises(TimeoutError):
            await events.wait_for("cleaning_finished", timeout=2)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_mock_orchestrator_conditional_routing(device_spawner, event_capture, mock_orchestrator):
    """Conditional routing: only route if class == 'mess'."""
    cam, cam_driver = await device_spawner.spawn_camera("itest-d2o-cond-cam")
    robot, _ = await device_spawner.spawn_robot("itest-d2o-cond-robot", clean_duration=0.3)

    mock_orchestrator.add_rule(
        on_event="state_change_detected",
        call_function="dispatch_robot",
        target_device_id="itest-d2o-cond-robot",
        condition=lambda data: data.get("class") == "mess",
    )

    async with event_capture.subscribe("device-connect.*.itest-d2o-cond-robot.event.*") as events:
        await asyncio.sleep(SETTLE_TIME)

        # 'clean' event should NOT trigger robot
        await cam_driver.trigger_event("state_change_detected", {"zone_id": "A", "class": "clean"})
        with pytest.raises(TimeoutError):
            await events.wait_for("cleaning_finished", timeout=1)

        # 'mess' event should trigger robot
        await cam_driver.trigger_event("state_change_detected", {"zone_id": "B", "class": "mess"})
        event = await events.wait_for("cleaning_finished", timeout=5)
        assert event.data["zone_id"] == "B"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_mock_orchestrator_param_transform(device_spawner, event_capture, mock_orchestrator):
    """Parameter transformation in routing rules."""
    cam, cam_driver = await device_spawner.spawn_camera("itest-d2o-xform-cam")
    robot, _ = await device_spawner.spawn_robot("itest-d2o-xform-robot", clean_duration=0.3)

    mock_orchestrator.add_rule(
        on_event="state_change_detected",
        call_function="dispatch_robot",
        target_device_id="itest-d2o-xform-robot",
        transform_params=lambda data: {"zone_id": data.get("zone_id", "default")},
    )

    async with event_capture.subscribe("device-connect.*.itest-d2o-xform-robot.event.*") as events:
        await asyncio.sleep(SETTLE_TIME)

        await cam_driver.trigger_event("state_change_detected", {"zone_id": "custom-zone", "class": "mess"})
        event = await events.wait_for("cleaning_finished", timeout=5)
        assert event.data["zone_id"] == "custom-zone"
