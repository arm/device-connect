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
async def test_broadcast_where_with_bindings(device_spawner, messaging_url):
    """A where predicate that reads bindings.<key> self-elects per-target."""
    pytest.importorskip("celpy")
    await device_spawner.spawn_camera("itest-bcbnd-cam-1", location="lab-A")
    await device_spawner.spawn_camera("itest-bcbnd-cam-2", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import (
        await_replies, broadcast, disconnect,
    )

    await _wait_for_devices(
        messaging_url, {"itest-bcbnd-cam-1", "itest-bcbnd-cam-2"}
    )
    try:
        # Allowlist sent in bindings; the predicate uses bindings.allow to
        # select. Devices not in the allowlist self-deselect silently.
        result = await asyncio.to_thread(
            broadcast,
            "device(itest-bcbnd-cam-*).function(capture_image)",
            None,
            "identity.device_id in bindings.allow",
            {"allow": ["itest-bcbnd-cam-1"]},
        )
        assert result["candidates"] == 2
        replies = await asyncio.to_thread(
            await_replies, result["correlation_id"], timeout=3.0,
        )
        ids = {r["device_id"] for r in replies}
        assert ids == {"itest-bcbnd-cam-1"}
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_await_replies_until_stops_early(device_spawner, messaging_url):
    """``await_replies`` returns once ``until`` replies have arrived."""
    await device_spawner.spawn_camera("itest-awu-cam-1", location="lab-A")
    await device_spawner.spawn_camera("itest-awu-cam-2", location="lab-A")
    await device_spawner.spawn_camera("itest-awu-cam-3", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import (
        await_replies, broadcast, disconnect,
    )

    await _wait_for_devices(
        messaging_url, {"itest-awu-cam-1", "itest-awu-cam-2", "itest-awu-cam-3"}
    )
    try:
        result = await asyncio.to_thread(
            broadcast, "device(itest-awu-cam-*).function(capture_image)",
        )
        assert result["candidates"] == 3
        # until=1 should let us return after the first reply arrives even
        # though more are coming.
        t0 = time.monotonic()
        replies = await asyncio.to_thread(
            await_replies, result["correlation_id"],
            timeout=5.0, until=1, poll_interval=0.02,
        )
        elapsed = time.monotonic() - t0
        assert len(replies) >= 1
        # Sanity: returning early should be well under the timeout.
        assert elapsed < 2.0, f"await_replies(until=1) took {elapsed:.2f}s"
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_subscribe_iter_protocol(device_spawner, messaging_url):
    """``for msg in sub:`` works via Subscription.__iter__."""
    await device_spawner.spawn_camera("itest-subiter-cam-1", location="lab-A")
    await device_spawner.spawn_camera("itest-subiter-cam-2", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import broadcast, disconnect, subscribe

    await _wait_for_devices(
        messaging_url, {"itest-subiter-cam-1", "itest-subiter-cam-2"}
    )
    try:
        result = await asyncio.to_thread(
            broadcast,
            "device(itest-subiter-cam-*).function(capture_image)",
        )
        cid = result["correlation_id"]

        def collect():
            # Exercise the bare ``for msg in sub:`` form (uses __iter__).
            # Break after both expected replies arrive so the test stays
            # bounded regardless of the default idle timeout.
            with subscribe(f"correlation:{cid}") as sub:
                gathered: list[dict] = []
                for msg in sub:
                    gathered.append(msg)
                    if len(gathered) >= 2:
                        break
                return gathered

        replies = await asyncio.to_thread(collect)
        ids = {r["device_id"] for r in replies}
        assert ids == {"itest-subiter-cam-1", "itest-subiter-cam-2"}
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_subscribe_event_selector_live_stream(device_spawner, messaging_url):
    """subscribe(event(<name>)) receives live events from matching devices."""
    device, driver = await device_spawner.spawn_camera(
        "itest-evsub-cam", location="lab-A",
    )
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, subscribe

    await _wait_for_devices(messaging_url, {"itest-evsub-cam"})
    try:
        with subscribe("device(itest-evsub-cam).event(object_detected)") as sub:
            await asyncio.sleep(SETTLE_TIME)  # let subscription warm up
            await driver.trigger_event(
                "object_detected",
                {"label": "person", "confidence": 0.95},
            )
            msgs = await asyncio.to_thread(
                list, sub.iter(timeout=2.0, poll_interval=0.05),
            )
            # The event arrives via the JSON-RPC event subject; payload is
            # under either ``params`` or top-level depending on transport.
            matching = [
                m for m in msgs
                if (m.get("params") or {}).get("label") == "person"
                or m.get("label") == "person"
            ]
            assert matching, f"no object_detected events received: {msgs}"
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


# -- PR 29 review #1: safety:critical advisory WARN ------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_broadcast_safety_critical_emits_advisory_warn_and_proceeds(
    device_spawner, messaging_url, caplog,
):
    """Broadcasting to a function tagged ``safety:critical`` emits an
    advisory WARN at the dispatcher and still publishes; the device
    receives the envelope and replies normally.

    Pinned end-to-end via a real backend so the advisory wiring isn't
    accidentally short-circuited by future refactors of the dispatch
    path. ``dispatch_robot`` on the production robot driver carries
    ``safety:critical`` (tests/drivers/robot.py).
    """
    import logging

    _TOOLS_LOGGER = "device_connect_agent_tools.tools"

    await device_spawner.spawn_robot("itest-bcsc-robot", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import await_replies, broadcast, disconnect

    await _wait_for_devices(messaging_url, {"itest-bcsc-robot"})
    caplog.set_level(logging.WARNING, logger=_TOOLS_LOGGER)
    try:
        result = await asyncio.to_thread(
            broadcast,
            "device(itest-bcsc-robot).function(dispatch_robot)",
            {"zone_id": "lab-A"},
        )
        # Advisory does not block — broadcast publishes and returns normally.
        assert "error" not in result
        assert result["correlation_id"].startswith("br-")
        assert result["function"] == "dispatch_robot"
        assert result["candidates"] == 1

        # Exactly one advisory WARN, naming the function and the device.
        criticals = [
            rec for rec in caplog.records
            if rec.name == _TOOLS_LOGGER
            and rec.levelno == logging.WARNING
            and "safety:critical" in rec.getMessage()
        ]
        assert len(criticals) == 1
        msg = criticals[0].getMessage()
        assert "dispatch_robot" in msg
        assert "itest-bcsc-robot" in msg
        assert result["correlation_id"] in msg

        # Reply path still works.
        replies = await asyncio.to_thread(
            await_replies, result["correlation_id"], timeout=5.0, until=1,
        )
        assert len(replies) == 1
        assert replies[0]["device_id"] == "itest-bcsc-robot"
    finally:
        await asyncio.to_thread(disconnect)
