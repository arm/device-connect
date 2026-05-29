# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Slow NATS-backed large-fleet tests for broadcast reply fan-out."""

import asyncio
import time
import uuid

import pytest

from fixtures.scale import scale_fleet_size, spawn_scale_sensor_fleet, wait_for_devices

SETTLE_TIME = 0.3
DISCOVERY_TIMEOUT = 45.0

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.timeout(240),
    pytest.mark.parametrize("messaging_backend", ["nats"], indirect=True),
]


def _reply_timeout(fleet_size: int) -> float:
    return max(15.0, min(60.0, fleet_size * 0.2))


def _spread_threshold(fleet_size: int) -> float:
    return max(1.0, min(2.5, fleet_size * 0.01))


def _collect_subscription_replies(correlation_id: str, expected: int, timeout: float):
    from device_connect_agent_tools import subscribe

    deadline = time.monotonic() + timeout
    gathered = []
    with subscribe(f"correlation:{correlation_id}") as sub:
        while len(gathered) < expected and time.monotonic() < deadline:
            gathered.extend(sub.read())
            if len(gathered) >= expected:
                break
            time.sleep(0.02)
    return gathered


async def test_broadcast_large_fan_out_returns_correlation_and_target_count(
    messaging_backend, messaging_url, clear_registry, device_spawner
):
    """broadcast() returns a correlation id and target count at fleet scale."""
    if messaging_backend != "nats":
        pytest.skip("large-fleet broadcast tests use registry-backed NATS discovery")

    fleet_size = scale_fleet_size()
    prefix = f"itest-bclarge-{uuid.uuid4().hex[:8]}"
    location = f"{prefix}-room"
    expected_ids = await spawn_scale_sensor_fleet(
        device_spawner,
        prefix=prefix,
        fleet_size=fleet_size,
        settle_time=SETTLE_TIME,
        location=location,
    )

    from device_connect_agent_tools import await_replies, broadcast, disconnect

    devices = await wait_for_devices(
        messaging_url,
        expected_ids,
        timeout=DISCOVERY_TIMEOUT,
    )
    assert expected_ids <= {d.get("device_id") for d in devices}

    try:
        result = await asyncio.to_thread(
            broadcast,
            f"device(location:{location}).function(get_reading)",
            {"unit": "celsius"},
        )
        assert result["correlation_id"].startswith("br-")
        assert result["candidates"] == fleet_size
        assert result["function"] == "get_reading"

        replies = await asyncio.to_thread(
            await_replies,
            result["correlation_id"],
            timeout=_reply_timeout(fleet_size),
            until=fleet_size,
            poll_interval=0.02,
        )
        assert len(replies) == fleet_size
        assert {reply["device_id"] for reply in replies} == expected_ids
        assert all(reply["success"] is True for reply in replies)
        assert all(reply["result"]["unit"] == "celsius" for reply in replies)
    finally:
        await asyncio.to_thread(disconnect)


async def test_broadcast_where_self_election_narrows_large_candidates(
    messaging_backend, messaging_url, clear_registry, device_spawner
):
    """A broad broadcast can be narrowed by edge-side where self-election."""
    if messaging_backend != "nats":
        pytest.skip("large-fleet broadcast tests use registry-backed NATS discovery")
    pytest.importorskip("celpy")

    fleet_size = scale_fleet_size()
    prefix = f"itest-bcwhere-{uuid.uuid4().hex[:8]}"
    selected_location = f"{prefix}-selected"
    other_location = f"{prefix}-other"

    def location_for(index):
        return selected_location if index % 4 == 0 else other_location

    expected_ids = await spawn_scale_sensor_fleet(
        device_spawner,
        prefix=prefix,
        fleet_size=fleet_size,
        settle_time=SETTLE_TIME,
        location_for=location_for,
    )
    selected_ids = {
        f"{prefix}-{i:04d}" for i in range(fleet_size) if i % 4 == 0
    }
    assert selected_ids

    from device_connect_agent_tools import await_replies, broadcast, disconnect

    devices = await wait_for_devices(
        messaging_url,
        expected_ids,
        timeout=DISCOVERY_TIMEOUT,
    )
    assert expected_ids <= {d.get("device_id") for d in devices}

    try:
        result = await asyncio.to_thread(
            broadcast,
            f"device({prefix}-*).function(get_reading)",
            {"unit": "celsius"},
            "labels.location == bindings.target_location",
            {"target_location": selected_location},
        )
        assert result["candidates"] == fleet_size

        replies = await asyncio.to_thread(
            await_replies,
            result["correlation_id"],
            timeout=_reply_timeout(fleet_size),
            until=len(selected_ids),
            poll_interval=0.02,
        )
        assert len(replies) == len(selected_ids)
        assert {reply["device_id"] for reply in replies} == selected_ids
        assert all(reply["success"] is True for reply in replies)
    finally:
        await asyncio.to_thread(disconnect)


async def test_broadcast_fire_at_synchronizes_large_fan_out(
    messaging_backend, messaging_url, clear_registry, device_spawner
):
    """fire_at keeps large fan-out reply fire times reasonably grouped."""
    if messaging_backend != "nats":
        pytest.skip("large-fleet broadcast tests use registry-backed NATS discovery")

    fleet_size = scale_fleet_size()
    prefix = f"itest-bcfire-{uuid.uuid4().hex[:8]}"
    location = f"{prefix}-room"
    expected_ids = await spawn_scale_sensor_fleet(
        device_spawner,
        prefix=prefix,
        fleet_size=fleet_size,
        settle_time=SETTLE_TIME,
        location=location,
    )

    from device_connect_agent_tools import await_replies, broadcast, disconnect

    devices = await wait_for_devices(
        messaging_url,
        expected_ids,
        timeout=DISCOVERY_TIMEOUT,
    )
    assert expected_ids <= {d.get("device_id") for d in devices}

    try:
        scheduled = time.time() + 1.5
        result = await asyncio.to_thread(
            broadcast,
            f"device(location:{location}).function(get_reading)",
            {"unit": "celsius"},
            None,
            None,
            scheduled,
            "fire",
        )
        assert result["candidates"] == fleet_size

        replies = await asyncio.to_thread(
            await_replies,
            result["correlation_id"],
            timeout=_reply_timeout(fleet_size),
            until=fleet_size,
            poll_interval=0.02,
        )
        assert len(replies) == fleet_size
        assert {reply["device_id"] for reply in replies} == expected_ids

        fire_times = [reply["actually_fired_at"] for reply in replies]
        assert min(fire_times) >= scheduled - 0.05
        spread = max(fire_times) - min(fire_times)
        assert spread < _spread_threshold(fleet_size), (
            f"fire_at spread too wide for {fleet_size} targets: {spread:.3f}s"
        )
    finally:
        await asyncio.to_thread(disconnect)


async def test_subscribe_correlation_drains_large_reply_stream(
    messaging_backend, messaging_url, clear_registry, device_spawner
):
    """subscribe('correlation:<id>') drains a large broadcast reply stream."""
    if messaging_backend != "nats":
        pytest.skip("large-fleet broadcast tests use registry-backed NATS discovery")

    fleet_size = scale_fleet_size()
    prefix = f"itest-bcsub-{uuid.uuid4().hex[:8]}"
    location = f"{prefix}-room"
    expected_ids = await spawn_scale_sensor_fleet(
        device_spawner,
        prefix=prefix,
        fleet_size=fleet_size,
        settle_time=SETTLE_TIME,
        location=location,
    )

    from device_connect_agent_tools import broadcast, disconnect

    devices = await wait_for_devices(
        messaging_url,
        expected_ids,
        timeout=DISCOVERY_TIMEOUT,
    )
    assert expected_ids <= {d.get("device_id") for d in devices}

    try:
        scheduled = time.time() + 1.5
        result = await asyncio.to_thread(
            broadcast,
            f"device(location:{location}).function(get_reading)",
            {"unit": "celsius"},
            None,
            None,
            scheduled,
            "fire",
        )
        assert result["candidates"] == fleet_size

        replies = await asyncio.to_thread(
            _collect_subscription_replies,
            result["correlation_id"],
            fleet_size,
            _reply_timeout(fleet_size),
        )
        assert len(replies) == fleet_size
        assert {reply["device_id"] for reply in replies} == expected_ids
        assert all(
            reply["correlation_id"] == result["correlation_id"]
            for reply in replies
        )
        assert all(reply["success"] is True for reply in replies)
    finally:
        await asyncio.to_thread(disconnect)
