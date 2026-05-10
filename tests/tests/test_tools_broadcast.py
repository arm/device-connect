# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for selector-driven broadcast + correlation replies.

End-to-end coverage for the async fan-out path:
- Dispatcher publishes a broadcast envelope on the fanout subject.
- Each device runtime self-elects via target_device_ids and the optional
  CEL ``where`` predicate.
- Devices execute the function and emit a reply on the per-device async
  reply subject keyed by correlation_id.
- ``await_replies`` collects replies for a bounded window.
"""

import asyncio
import time

import pytest

SETTLE_TIME = 0.4
DISCOVERY_TIMEOUT = 5.0


async def _wait_for_devices(messaging_url, expected_ids):
    from device_connect_agent_tools import connect
    from device_connect_agent_tools.connection import get_connection

    await asyncio.to_thread(connect, nats_url=messaging_url)
    deadline = time.monotonic() + DISCOVERY_TIMEOUT
    while True:
        conn = get_connection()
        devices = await asyncio.to_thread(conn.list_devices)
        ids = {d.get("device_id") for d in devices}
        if expected_ids.issubset(ids) or time.monotonic() > deadline:
            return devices
        await asyncio.sleep(0.25)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_broadcast_returns_correlation_and_replies_arrive(
    device_spawner, messaging_url,
):
    """broadcast() returns a correlation_id and matching devices reply on the
    per-device async reply subject."""
    await device_spawner.spawn_camera("itest-bc-cam-1", location="lab-A")
    await device_spawner.spawn_camera("itest-bc-cam-2", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import (
        await_replies, broadcast, disconnect,
    )

    await _wait_for_devices(messaging_url, {"itest-bc-cam-1", "itest-bc-cam-2"})
    try:
        result = await asyncio.to_thread(
            broadcast,
            "device(itest-bc-cam-*).function(capture_image)",
            {"resolution": "720p"},
        )
        assert result["correlation_id"].startswith("br-")
        assert result["candidates"] == 2
        assert result["function"] == "capture_image"

        replies = await asyncio.to_thread(
            await_replies, result["correlation_id"], timeout=5.0, until=2,
        )
        assert len(replies) == 2
        ids = {r["device_id"] for r in replies}
        assert ids == {"itest-bc-cam-1", "itest-bc-cam-2"}
        for r in replies:
            assert r["success"] is True
            assert r["correlation_id"] == result["correlation_id"]
            assert "actually_fired_at" in r
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_broadcast_where_filters_at_edge(device_spawner, messaging_url):
    """A CEL where predicate runs at each candidate; only matches reply."""
    pytest.importorskip("celpy")
    await device_spawner.spawn_camera("itest-bcw-cam-a", location="lab-A")
    await device_spawner.spawn_camera("itest-bcw-cam-b", location="lab-B")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import (
        await_replies, broadcast, disconnect,
    )

    await _wait_for_devices(messaging_url, {"itest-bcw-cam-a", "itest-bcw-cam-b"})
    try:
        result = await asyncio.to_thread(
            broadcast,
            "device(itest-bcw-cam-*).function(capture_image)",
            {"resolution": "1080p"},
            "labels.location == 'lab-A'",  # where predicate
        )
        assert result["candidates"] == 2

        replies = await asyncio.to_thread(
            await_replies, result["correlation_id"], timeout=3.0,
        )
        # Only cam-a is in lab-A; cam-b silently self-deselects.
        ids = {r["device_id"] for r in replies}
        assert ids == {"itest-bcw-cam-a"}
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_broadcast_fire_at_synchronizes_fan_out(
    device_spawner, messaging_url,
):
    """fire_at causes each device to fire from its own clock at the deadline."""
    await device_spawner.spawn_camera("itest-bcf-cam-1", location="lab-A")
    await device_spawner.spawn_camera("itest-bcf-cam-2", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import (
        await_replies, broadcast, disconnect,
    )

    await _wait_for_devices(messaging_url, {"itest-bcf-cam-1", "itest-bcf-cam-2"})
    try:
        # Schedule 0.5s in the future; on_late=skip so any tardy device drops
        # the call rather than firing late and breaking the coherence.
        scheduled = time.time() + 0.5
        result = await asyncio.to_thread(
            broadcast,
            "device(itest-bcf-cam-*).function(capture_image)",
            None, None, None,
            scheduled,  # fire_at
            "skip",     # on_late
        )
        assert result["candidates"] == 2

        replies = await asyncio.to_thread(
            await_replies, result["correlation_id"], timeout=3.0, until=2,
        )
        assert len(replies) == 2
        # actually_fired_at should be at-or-after the scheduled time on each.
        for r in replies:
            assert r["actually_fired_at"] >= scheduled - 0.05  # small slack
        # Achieved spread should be tight (well under network jitter).
        spread = max(r["actually_fired_at"] for r in replies) - min(
            r["actually_fired_at"] for r in replies
        )
        assert spread < 0.5, f"fire_at spread too wide: {spread:.3f}s"
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_broadcast_fire_at_late_with_skip_drops(
    device_spawner, messaging_url,
):
    """A fire_at in the past with on_late=skip yields no replies."""
    await device_spawner.spawn_camera("itest-bcl-cam", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import (
        await_replies, broadcast, disconnect,
    )

    await _wait_for_devices(messaging_url, {"itest-bcl-cam"})
    try:
        past = time.time() - 5.0  # already 5s late
        result = await asyncio.to_thread(
            broadcast,
            "device(itest-bcl-cam).function(capture_image)",
            None, None, None, past, "skip",
        )
        replies = await asyncio.to_thread(
            await_replies, result["correlation_id"], timeout=1.5,
        )
        assert replies == []
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_subscribe_correlation_form(device_spawner, messaging_url):
    """subscribe('correlation:<id>') captures replies as they arrive."""
    await device_spawner.spawn_camera("itest-bcs-cam-1", location="lab-A")
    await device_spawner.spawn_camera("itest-bcs-cam-2", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import broadcast, disconnect, subscribe

    await _wait_for_devices(messaging_url, {"itest-bcs-cam-1", "itest-bcs-cam-2"})
    try:
        result = await asyncio.to_thread(
            broadcast,
            "device(itest-bcs-cam-*).function(capture_image)",
        )
        cid = result["correlation_id"]

        def collect():
            with subscribe(f"correlation:{cid}") as sub:
                # Drain over a short window.
                return list(sub.iter(timeout=2.0, poll_interval=0.05))

        replies = await asyncio.to_thread(collect)
        ids = {r["device_id"] for r in replies}
        assert ids == {"itest-bcs-cam-1", "itest-bcs-cam-2"}
    finally:
        await asyncio.to_thread(disconnect)
