# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Large-fleet integration tests for heterogeneous discovery and operations."""

import asyncio
import os
import time
import uuid

import pytest

SETTLE_TIME = 0.3
DISCOVERY_TIMEOUT = 60.0
DEFAULT_SCALE_FLEET_SIZE = 200
CAMERA_COUNT = 3
ROBOT_COUNT = 2

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.timeout(300),
    pytest.mark.parametrize("messaging_backend", ["nats"], indirect=True),
]


def _scale_fleet_size() -> int:
    return max(12, int(os.getenv("DC_SCALE_FLEET_SIZE", str(DEFAULT_SCALE_FLEET_SIZE))))


def _reply_timeout(fleet_size: int) -> float:
    return max(15.0, min(60.0, fleet_size * 0.2))


async def _wait_for_devices(messaging_url, expected_ids, timeout=DISCOVERY_TIMEOUT):
    """Connect and poll until all expected device ids are visible."""
    from device_connect_agent_tools import connect
    from device_connect_agent_tools.connection import get_connection

    await asyncio.to_thread(connect, nats_url=messaging_url)
    deadline = time.monotonic() + timeout
    while True:
        conn = get_connection()
        conn.invalidate_cache()
        devices = await asyncio.to_thread(conn.list_devices)
        ids = {d.get("device_id") for d in devices}
        if expected_ids <= ids or time.monotonic() > deadline:
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


async def _spawn_heterogeneous_fleet(device_spawner, prefix: str):
    fleet_size = _scale_fleet_size()
    sensor_count = fleet_size - CAMERA_COUNT - ROBOT_COUNT
    location = f"{prefix}-mixed-room"

    sensor_ids = {f"{prefix}-sensor-{i:04d}" for i in range(sensor_count)}
    camera_ids = {f"{prefix}-cam-{i:04d}" for i in range(CAMERA_COUNT)}
    robot_ids = {f"{prefix}-robot-{i:04d}" for i in range(ROBOT_COUNT)}

    await device_spawner.spawn_sensor_fleet(
        f"{prefix}-sensor",
        sensor_count,
        location=location,
        initial_temp=21.0,
        registration_timeout=45.0,
    )
    await asyncio.gather(*[
        device_spawner.spawn_camera(device_id, location=location)
        for device_id in sorted(camera_ids)
    ])
    await asyncio.gather(*[
        device_spawner.spawn_robot(device_id, location=location)
        for device_id in sorted(robot_ids)
    ])
    await asyncio.sleep(SETTLE_TIME)

    return {
        "fleet_size": fleet_size,
        "sensor_count": sensor_count,
        "camera_count": CAMERA_COUNT,
        "robot_count": ROBOT_COUNT,
        "location": location,
        "sensor_ids": sensor_ids,
        "camera_ids": camera_ids,
        "robot_ids": robot_ids,
        "all_ids": sensor_ids | camera_ids | robot_ids,
    }


async def test_heterogeneous_discovery_outputs_expected_matrix(
    messaging_backend, messaging_url, clear_registry, device_spawner
):
    """Mixed device/function discovery returns the expected output contract."""
    if messaging_backend != "nats":
        pytest.skip("large heterogeneous discovery uses registry-backed NATS discovery")

    from device_connect_agent_tools import disconnect, discover, discover_labels
    from device_connect_agent_tools.tools import DC_FUNCTION_THRESHOLD

    prefix = f"itest-lfh-disc-{uuid.uuid4().hex[:8]}"
    fleet = await _spawn_heterogeneous_fleet(device_spawner, prefix)
    await _wait_for_devices(messaging_url, fleet["all_ids"])

    try:
        labels = await asyncio.to_thread(discover_labels)
        device_categories = labels["device_keys"]["category"]["values"]
        function_direction = labels["function_keys"]["direction"]["values"]
        function_modality = labels["function_keys"]["modality"]["values"]
        function_safety = labels["function_keys"]["safety"]["values"]

        assert device_categories["sensor"] == fleet["sensor_count"]
        assert device_categories["camera"] == fleet["camera_count"]
        assert device_categories["robot"] == fleet["robot_count"]
        assert labels["device_keys"]["location"]["values"][fleet["location"]] == (
            fleet["fleet_size"]
        )
        assert function_direction["read"] == fleet["sensor_count"] + fleet["robot_count"]
        assert function_direction["write"] == (
            fleet["sensor_count"] * 2 + fleet["camera_count"] + fleet["robot_count"]
        )
        assert function_modality["thermal"] == fleet["sensor_count"]
        assert function_modality["rgb"] == fleet["camera_count"]
        assert function_safety["critical"] == (
            fleet["sensor_count"] + fleet["robot_count"]
        )

        cases = [
            {
                "name": "read functions exclude cameras",
                "selector": f"device(location:{fleet['location']}).function(direction:read)",
                "expected_pairs": {
                    *((device_id, "get_reading") for device_id in fleet["sensor_ids"]),
                    *((device_id, "get_status") for device_id in fleet["robot_ids"]),
                },
                "histogram": {
                    ("direction", "read"): fleet["sensor_count"] + fleet["robot_count"],
                    ("modality", "thermal"): fleet["sensor_count"],
                },
            },
            {
                "name": "camera writes stay isolated",
                "selector": (
                    f"device(category:camera,location:{fleet['location']})"
                    ".function(direction:write)"
                ),
                "expected_pairs": {
                    (device_id, "capture_image") for device_id in fleet["camera_ids"]
                },
                "histogram": {
                    ("direction", "write"): fleet["camera_count"],
                    ("modality", "rgb"): fleet["camera_count"],
                },
            },
            {
                "name": "critical sensor writes exclude robot dispatch",
                "selector": (
                    f"device(category:sensor,location:{fleet['location']})"
                    ".function(direction:write,safety:critical)"
                ),
                "expected_pairs": {
                    (device_id, "set_threshold") for device_id in fleet["sensor_ids"]
                },
                "histogram": {
                    ("direction", "write"): fleet["sensor_count"],
                    ("safety", "critical"): fleet["sensor_count"],
                },
            },
        ]

        for case in cases:
            result = await asyncio.to_thread(
                discover,
                case["selector"],
                0,
                fleet["fleet_size"] * 3,
            )
            pairs = {(row["device_id"], row["name"]) for row in result["results"]}
            assert result["scope"] == "device_function", case["name"]
            assert result["matched"] == len(case["expected_pairs"]), case["name"]
            assert result["returned"] == len(case["expected_pairs"]), case["name"]
            assert pairs == case["expected_pairs"], case["name"]

            for (key, value), count in case["histogram"].items():
                assert result["label_histogram"][key]["values"][value] == count

            if result["matched"] <= DC_FUNCTION_THRESHOLD:
                _assert_expanded_function_rows(result["results"])
            else:
                _assert_compact_function_rows(result["results"])

        broad = await asyncio.to_thread(
            discover,
            f"device(location:{fleet['location']}).function(*)",
            0,
            40,
        )
        assert broad["matched"] == (
            fleet["sensor_count"] * 3 + fleet["camera_count"] + fleet["robot_count"] * 2
        )
        assert broad["returned"] == 40
        assert broad["next_offset"] == 40
        _assert_compact_function_rows(broad["results"])
        assert broad["label_histogram"]["direction"]["values"]["read"] == (
            fleet["sensor_count"] + fleet["robot_count"]
        )
        assert broad["label_histogram"]["direction"]["values"]["write"] == (
            fleet["sensor_count"] * 2 + fleet["camera_count"] + fleet["robot_count"]
        )
        assert broad["label_histogram"]["modality"]["values"]["thermal"] == (
            fleet["sensor_count"]
        )
        assert broad["label_histogram"]["modality"]["values"]["rgb"] == (
            fleet["camera_count"]
        )
        assert broad["label_histogram"]["safety"]["values"]["critical"] == (
            fleet["sensor_count"] + fleet["robot_count"]
        )
    finally:
        await asyncio.to_thread(disconnect)


async def test_heterogeneous_invoke_many_targets_only_matching_functions(
    messaging_backend, messaging_url, clear_registry, device_spawner
):
    """invoke_many ignores unrelated devices in a mixed fleet."""
    if messaging_backend != "nats":
        pytest.skip("large heterogeneous invoke_many uses registry-backed NATS discovery")

    from device_connect_agent_tools import disconnect, invoke_many

    prefix = f"itest-lfh-inv-{uuid.uuid4().hex[:8]}"
    fleet = await _spawn_heterogeneous_fleet(device_spawner, prefix)
    await _wait_for_devices(messaging_url, fleet["all_ids"])

    try:
        result = await asyncio.to_thread(
            invoke_many,
            f"device(location:{fleet['location']}).function(get_reading)",
            {"unit": "celsius"},
            10.0,
            64,
            "heterogeneous invoke_many selector isolation test",
        )
        assert result["candidates"] == fleet["sensor_count"]
        assert result["matched"] == fleet["sensor_count"]
        assert result["succeeded"] == fleet["sensor_count"]
        assert result["failed"] == 0
        assert {row["device_id"] for row in result["results"]} == fleet["sensor_ids"]
        assert all(row["function"] == "get_reading" for row in result["results"])
        assert all(row["result"]["unit"] == "celsius" for row in result["results"])
    finally:
        await asyncio.to_thread(disconnect)


async def test_heterogeneous_broadcast_replies_only_from_matching_functions(
    messaging_backend, messaging_url, clear_registry, device_spawner
):
    """broadcast resolves a mixed fleet to the matching function subset."""
    if messaging_backend != "nats":
        pytest.skip("large heterogeneous broadcast uses registry-backed NATS discovery")

    from device_connect_agent_tools import await_replies, broadcast, disconnect

    prefix = f"itest-lfh-bc-{uuid.uuid4().hex[:8]}"
    fleet = await _spawn_heterogeneous_fleet(device_spawner, prefix)
    await _wait_for_devices(messaging_url, fleet["all_ids"])

    try:
        result = await asyncio.to_thread(
            broadcast,
            f"device(location:{fleet['location']}).function(get_reading)",
            {"unit": "celsius"},
        )
        assert result["correlation_id"].startswith("br-")
        assert result["candidates"] == fleet["sensor_count"]
        assert result["function"] == "get_reading"

        replies = await asyncio.to_thread(
            await_replies,
            result["correlation_id"],
            timeout=_reply_timeout(fleet["sensor_count"]),
            until=fleet["sensor_count"],
            poll_interval=0.02,
        )
        assert len(replies) == fleet["sensor_count"]
        assert {reply["device_id"] for reply in replies} == fleet["sensor_ids"]
        assert all(reply["success"] is True for reply in replies)
        assert all(reply["result"]["unit"] == "celsius" for reply in replies)
    finally:
        await asyncio.to_thread(disconnect)
