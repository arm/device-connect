# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Slow NATS-only large-fleet discovery integration tests."""

import asyncio
import uuid

import pytest

from fixtures.scale import (
    assert_device_row_compact,
    assert_device_row_expanded,
    scale_fleet_size,
    wait_for_devices,
)

SETTLE_TIME = 0.3
DISCOVERY_TIMEOUT = 60.0

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.timeout(240),
    pytest.mark.parametrize("messaging_backend", ["nats"], indirect=True),
]


async def test_large_device_set_summary_does_not_expand_full_schemas(
    messaging_backend, messaging_url, clear_registry, device_spawner
):
    """A large matched device selector stays compact but keeps counts and labels."""
    fleet_size = scale_fleet_size(minimum=6)
    prefix = f"itest-lfs-{uuid.uuid4().hex[:8]}"
    location = f"{prefix}-room"
    expected_ids = {f"{prefix}-{i:04d}" for i in range(fleet_size)}

    await device_spawner.spawn_sensor_fleet(
        prefix,
        fleet_size,
        location=location,
        registration_timeout=30.0,
    )
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover

    await wait_for_devices(
        messaging_url,
        expected_ids,
        timeout=DISCOVERY_TIMEOUT,
        invalidate_cache=True,
    )
    try:
        page_size = min(25, fleet_size - 1)
        result = await asyncio.to_thread(
            discover, f"device(location:{location})", 0, page_size
        )

        assert result["scope"] == "device_only"
        assert result["matched"] == fleet_size
        assert result["returned"] == page_size
        assert result["next_offset"] == page_size
        assert result["label_histogram"]["location"]["values"][location] == fleet_size
        assert result["label_histogram"]["category"]["values"]["sensor"] == fleet_size
        for row in result["results"]:
            assert_device_row_compact(row)
    finally:
        await asyncio.to_thread(disconnect)


async def test_small_matched_subset_expands_inside_large_global_fleet(
    messaging_backend, messaging_url, clear_registry, device_spawner
):
    """Expansion is based on matched selector cardinality, not global size."""
    total_size = scale_fleet_size(minimum=8)
    sensor_count = total_size - 2
    prefix = f"itest-lfm-{uuid.uuid4().hex[:8]}"
    large_location = f"{prefix}-bulk"
    small_location = f"{prefix}-inspection"
    sensor_ids = {f"{prefix}-{i:04d}" for i in range(sensor_count)}
    camera_ids = {f"{prefix}-cam-{i}" for i in range(2)}

    await device_spawner.spawn_sensor_fleet(
        prefix,
        sensor_count,
        location=large_location,
        registration_timeout=30.0,
    )
    for device_id in sorted(camera_ids):
        await device_spawner.spawn_camera(device_id, location=small_location)
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover

    await wait_for_devices(
        messaging_url,
        sensor_ids | camera_ids,
        timeout=DISCOVERY_TIMEOUT,
        invalidate_cache=True,
    )
    try:
        large = await asyncio.to_thread(
            discover, f"device(location:{large_location})", 0, 10
        )
        small = await asyncio.to_thread(
            discover, f"device(location:{small_location})", 0, 10
        )

        assert large["matched"] == sensor_count
        assert large["returned"] == 10
        for row in large["results"]:
            assert_device_row_compact(row)

        assert small["matched"] == 2
        assert small["returned"] == 2
        assert {row["device_id"] for row in small["results"]} == camera_ids
        for row in small["results"]:
            assert_device_row_expanded(row, "capture_image")
    finally:
        await asyncio.to_thread(disconnect)


async def test_long_tail_label_histogram_reports_more(
    messaging_backend, messaging_url, clear_registry, device_spawner
):
    """The multi-axis vocabulary truncates long-tail values and reports more."""
    from device_connect_agent_tools import disconnect, discover_labels
    from device_connect_agent_tools import tools as tools_mod

    top_n = max(1, min(tools_mod.LABEL_VALUES_TOP_N, 200))
    fleet_size = scale_fleet_size(minimum=top_n + 1)
    prefix = f"itest-lfl-{uuid.uuid4().hex[:8]}"
    expected_ids = {f"{prefix}-{i:04d}" for i in range(fleet_size)}

    await device_spawner.spawn_sensor_fleet(
        prefix,
        fleet_size,
        location_for=lambda i: f"{prefix}-zone-{i:04d}",
        registration_timeout=30.0,
    )
    await asyncio.sleep(SETTLE_TIME)

    await wait_for_devices(
        messaging_url,
        expected_ids,
        timeout=DISCOVERY_TIMEOUT,
        invalidate_cache=True,
    )
    try:
        result = await asyncio.to_thread(discover_labels)
        location = result["device_keys"]["location"]

        assert result["total_devices"] >= fleet_size
        assert len(location["values"]) == top_n
        assert location["more"] >= 1
    finally:
        await asyncio.to_thread(disconnect)


async def test_per_key_label_drill_down_bypasses_truncation(
    messaging_backend, messaging_url, clear_registry, device_spawner
):
    """The per-key vocabulary form paginates every value and omits more."""
    from device_connect_agent_tools import disconnect, discover_labels
    from device_connect_agent_tools import tools as tools_mod

    top_n = max(1, min(tools_mod.LABEL_VALUES_TOP_N, 200))
    fleet_size = scale_fleet_size(minimum=top_n + 1)
    prefix = f"itest-lfp-{uuid.uuid4().hex[:8]}"
    expected_ids = {f"{prefix}-{i:04d}" for i in range(fleet_size)}
    expected_locations = {f"{prefix}-zone-{i:04d}" for i in range(fleet_size)}

    await device_spawner.spawn_sensor_fleet(
        prefix,
        fleet_size,
        location_for=lambda i: f"{prefix}-zone-{i:04d}",
        registration_timeout=30.0,
    )
    await asyncio.sleep(SETTLE_TIME)

    await wait_for_devices(
        messaging_url,
        expected_ids,
        timeout=DISCOVERY_TIMEOUT,
        invalidate_cache=True,
    )
    try:
        seen = {}
        offset = 0
        limit = 7
        while True:
            page = await asyncio.to_thread(
                discover_labels, "device.location", offset, limit
            )
            assert page["axis"] == "device"
            assert page["key"] == "location"
            assert page["matched"] >= fleet_size
            assert page["axis_total"] >= fleet_size
            assert "more" not in page
            seen.update(page["values"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]

        assert expected_locations <= set(seen)
        assert all(seen[location] == 1 for location in expected_locations)
    finally:
        await asyncio.to_thread(disconnect)


async def test_device_label_or_and_filtering_over_large_fleet(
    messaging_backend, messaging_url, clear_registry, device_spawner
):
    """Device label OR within a key and AND across keys scale together."""
    total_size = scale_fleet_size(minimum=12)
    sensor_count = total_size - 5
    prefix = f"itest-lfo-{uuid.uuid4().hex[:8]}"
    loc_a = f"{prefix}-alpha"
    loc_b = f"{prefix}-bravo"
    sensor_ids = {f"{prefix}-{i:04d}" for i in range(sensor_count)}
    sensor_a_ids = {f"{prefix}-{i:04d}" for i in range(0, sensor_count, 2)}
    camera_ids = {f"{prefix}-cam-{i}" for i in range(3)}
    robot_ids = {f"{prefix}-robot-{i}" for i in range(2)}

    await device_spawner.spawn_sensor_fleet(
        prefix,
        sensor_count,
        location_for=lambda i: loc_a if i % 2 == 0 else loc_b,
        registration_timeout=30.0,
    )
    await device_spawner.spawn_camera(f"{prefix}-cam-0", location=loc_a)
    await device_spawner.spawn_camera(f"{prefix}-cam-1", location=loc_a)
    await device_spawner.spawn_camera(f"{prefix}-cam-2", location=loc_b)
    await device_spawner.spawn_robot(f"{prefix}-robot-0", location=loc_b)
    await device_spawner.spawn_robot(f"{prefix}-robot-1", location=loc_b)
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover

    await wait_for_devices(
        messaging_url,
        sensor_ids | camera_ids | robot_ids,
        timeout=DISCOVERY_TIMEOUT,
        invalidate_cache=True,
    )
    try:
        category_or = await asyncio.to_thread(
            discover,
            f"device(category:[sensor,camera],location:[{loc_a},{loc_b}])",
            0,
            total_size,
        )
        category_or_ids = {row["device_id"] for row in category_or["results"]}
        assert category_or["matched"] == sensor_count + len(camera_ids)
        assert category_or_ids == sensor_ids | camera_ids
        assert category_or_ids.isdisjoint(robot_ids)

        sensor_at_loc_a = await asyncio.to_thread(
            discover, f"device(category:sensor,location:{loc_a})", 0, sensor_count
        )
        assert sensor_at_loc_a["matched"] == len(sensor_a_ids)
        assert {row["device_id"] for row in sensor_at_loc_a["results"]} == sensor_a_ids

        mobile_at_loc_b = await asyncio.to_thread(
            discover, f"device(category:[camera,robot],location:{loc_b})", 0, 10
        )
        assert mobile_at_loc_b["matched"] == 3
        assert {row["device_id"] for row in mobile_at_loc_b["results"]} == {
            f"{prefix}-cam-2",
            f"{prefix}-robot-0",
            f"{prefix}-robot-1",
        }
    finally:
        await asyncio.to_thread(disconnect)


async def test_function_label_selection_over_heterogeneous_fleet(
    messaging_backend, messaging_url, clear_registry, device_spawner
):
    """Function-label selectors return the expected heterogeneous matrix."""
    total_size = scale_fleet_size(minimum=12)
    sensor_count = total_size - 4
    prefix = f"itest-lff-{uuid.uuid4().hex[:8]}"
    location = f"{prefix}-floor"
    sensor_ids = {f"{prefix}-{i:04d}" for i in range(sensor_count)}
    camera_ids = {f"{prefix}-cam-{i}" for i in range(2)}
    robot_ids = {f"{prefix}-robot-{i}" for i in range(2)}

    await device_spawner.spawn_sensor_fleet(
        prefix,
        sensor_count,
        location=location,
        registration_timeout=30.0,
    )
    for device_id in sorted(camera_ids):
        await device_spawner.spawn_camera(device_id, location=location)
    for device_id in sorted(robot_ids):
        await device_spawner.spawn_robot(device_id, location=location)
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover

    await wait_for_devices(
        messaging_url,
        sensor_ids | camera_ids | robot_ids,
        timeout=DISCOVERY_TIMEOUT,
        invalidate_cache=True,
    )
    try:
        read_selector = f"device(location:{location}).function(direction:read)"
        read_functions = await asyncio.to_thread(
            discover, read_selector, 0, total_size + 10
        )
        read_pairs = {
            (row["device_id"], row["name"]) for row in read_functions["results"]
        }
        assert read_functions["matched"] == sensor_count + len(robot_ids)
        assert read_pairs == {
            *((device_id, "get_reading") for device_id in sensor_ids),
            *((device_id, "get_status") for device_id in robot_ids),
        }
        assert not any(
            row["device_id"] in camera_ids for row in read_functions["results"]
        )

        camera_writes = await asyncio.to_thread(
            discover,
            f"device(category:camera,location:{location}).function(direction:write)",
            0,
            10,
        )
        assert camera_writes["matched"] == len(camera_ids)
        assert {
            (row["device_id"], row["name"]) for row in camera_writes["results"]
        } == {(device_id, "capture_image") for device_id in camera_ids}

        critical_sensor_selector = (
            f"device(category:sensor,location:{location})"
            ".function(direction:write,safety:critical)"
        )
        critical_sensor_writes = await asyncio.to_thread(
            discover, critical_sensor_selector, 0, sensor_count + 1
        )
        assert critical_sensor_writes["matched"] == sensor_count
        assert {
            (row["device_id"], row["name"])
            for row in critical_sensor_writes["results"]
        } == {(device_id, "set_threshold") for device_id in sensor_ids}
    finally:
        await asyncio.to_thread(disconnect)
