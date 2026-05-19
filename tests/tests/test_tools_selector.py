# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for selector-driven discovery tools.

Covers ``discover()`` and ``discover_labels()`` against real devices
registered via the messaging backend. Exercises the full selector grammar
end-to-end across all five scope shapes (device / device.function /
device.event / function / event), label filters (category, location,
direction, modality, safety), pagination, and the legacy-location mirror.
"""

import asyncio
import time

import pytest

SETTLE_TIME = 0.3
DISCOVERY_TIMEOUT = 5.0


async def _wait_for_devices(messaging_url, expected_ids):
    """Connect and poll until all expected ``device_ids`` are visible.

    Returns the list of flattened device dicts. Caller is responsible for
    disconnecting.
    """
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


# -- discover: device-only scope ---------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_wildcard_returns_all_devices(device_spawner, messaging_url):
    """``discover('device(*)')`` returns the full roster."""
    await device_spawner.spawn_camera("itest-sel-all-cam", location="lab-A")
    await device_spawner.spawn_sensor("itest-sel-all-sensor", location="lab-B")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover

    await _wait_for_devices(messaging_url, {"itest-sel-all-cam", "itest-sel-all-sensor"})
    try:
        result = await asyncio.to_thread(discover, "device(*)")
        assert result["scope"] == "device_only"
        assert result["matched"] >= 2
        ids = {d["device_id"] for d in result["results"]}
        assert {"itest-sel-all-cam", "itest-sel-all-sensor"} <= ids
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_by_device_id(device_spawner, messaging_url):
    """A bare-id selector resolves to one device."""
    await device_spawner.spawn_camera("itest-sel-id-cam", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover

    await _wait_for_devices(messaging_url, {"itest-sel-id-cam"})
    try:
        result = await asyncio.to_thread(discover, "device(itest-sel-id-cam)")
        assert result["scope"] == "device_only"
        assert result["matched"] == 1
        assert result["results"][0]["device_id"] == "itest-sel-id-cam"
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_by_id_glob(device_spawner, messaging_url):
    """Bare-id selectors accept globs (anchored fnmatch)."""
    await device_spawner.spawn_camera("itest-sel-glob-cam-1", location="lab-A")
    await device_spawner.spawn_camera("itest-sel-glob-cam-2", location="lab-A")
    await device_spawner.spawn_sensor("itest-sel-glob-sensor", location="lab-B")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover

    await _wait_for_devices(
        messaging_url,
        {"itest-sel-glob-cam-1", "itest-sel-glob-cam-2", "itest-sel-glob-sensor"},
    )
    try:
        result = await asyncio.to_thread(discover, "device(itest-sel-glob-cam-*)")
        ids = {d["device_id"] for d in result["results"]}
        assert "itest-sel-glob-cam-1" in ids
        assert "itest-sel-glob-cam-2" in ids
        assert "itest-sel-glob-sensor" not in ids
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_by_category_label(device_spawner, messaging_url):
    """``device(category:camera)`` returns only cameras (label-based)."""
    await device_spawner.spawn_camera("itest-sel-cat-cam", location="lab-A")
    await device_spawner.spawn_robot("itest-sel-cat-robot", location="lab-A")
    await device_spawner.spawn_sensor("itest-sel-cat-sensor", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover

    await _wait_for_devices(
        messaging_url,
        {"itest-sel-cat-cam", "itest-sel-cat-robot", "itest-sel-cat-sensor"},
    )
    try:
        result = await asyncio.to_thread(discover, "device(category:camera)")
        ids = {d["device_id"] for d in result["results"]}
        assert "itest-sel-cat-cam" in ids
        assert "itest-sel-cat-robot" not in ids
        assert "itest-sel-cat-sensor" not in ids
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_or_within_category(device_spawner, messaging_url):
    """Bracket lists OR within a key: cameras or robots, not sensors."""
    await device_spawner.spawn_camera("itest-sel-or-cam", location="lab-A")
    await device_spawner.spawn_robot("itest-sel-or-robot", location="lab-A")
    await device_spawner.spawn_sensor("itest-sel-or-sensor", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover

    await _wait_for_devices(
        messaging_url,
        {"itest-sel-or-cam", "itest-sel-or-robot", "itest-sel-or-sensor"},
    )
    try:
        result = await asyncio.to_thread(
            discover, "device(category:[camera,robot])"
        )
        ids = {d["device_id"] for d in result["results"]}
        assert "itest-sel-or-cam" in ids
        assert "itest-sel-or-robot" in ids
        assert "itest-sel-or-sensor" not in ids
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_and_across_category_and_location(
    device_spawner, messaging_url
):
    """Comma is AND across keys: category=camera AND location=lab-A."""
    await device_spawner.spawn_camera("itest-sel-and-cam-a", location="lab-A")
    await device_spawner.spawn_camera("itest-sel-and-cam-b", location="lab-B")
    await device_spawner.spawn_robot("itest-sel-and-robot-a", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover

    await _wait_for_devices(
        messaging_url,
        {"itest-sel-and-cam-a", "itest-sel-and-cam-b", "itest-sel-and-robot-a"},
    )
    try:
        result = await asyncio.to_thread(
            discover, "device(category:camera, location:lab-A)"
        )
        ids = {d["device_id"] for d in result["results"]}
        assert "itest-sel-and-cam-a" in ids
        assert "itest-sel-and-cam-b" not in ids  # wrong location
        assert "itest-sel-and-robot-a" not in ids  # wrong category
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_by_location_via_legacy_mirror(device_spawner, messaging_url):
    """Legacy ``DeviceStatus.location`` is mirrored into ``labels['location']``.

    The flatten_device location-mirror lifts ``status.location`` into
    ``labels['location']`` when ``capabilities.labels`` does not declare
    one, so selector queries on location work even for drivers that only
    populate the legacy heartbeat field.
    """
    await device_spawner.spawn_camera("itest-sel-mirror-cam", location="lab-A")
    await device_spawner.spawn_sensor("itest-sel-mirror-sensor", location="lab-B")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover

    await _wait_for_devices(
        messaging_url, {"itest-sel-mirror-cam", "itest-sel-mirror-sensor"}
    )
    try:
        result = await asyncio.to_thread(discover, "device(location:lab-A)")
        ids = {d["device_id"] for d in result["results"]}
        assert "itest-sel-mirror-cam" in ids
        assert "itest-sel-mirror-sensor" not in ids
    finally:
        await asyncio.to_thread(disconnect)


# -- discover: function-scoped --------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_function_scope_per_device(device_spawner, messaging_url):
    """``device(<id>).function(*)`` returns a device's RPC roster."""
    await device_spawner.spawn_camera("itest-sel-fn-cam", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover

    await _wait_for_devices(messaging_url, {"itest-sel-fn-cam"})
    try:
        result = await asyncio.to_thread(
            discover, "device(itest-sel-fn-cam).function(*)"
        )
        assert result["scope"] == "device_function"
        names = {row.get("name") for row in result["results"]}
        assert "capture_image" in names
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_function_by_name_fleet_wide(device_spawner, messaging_url):
    """``device(*).function(<name>)`` returns ``(device, function)`` tuples."""
    await device_spawner.spawn_camera("itest-sel-fnname-cam-1", location="lab-A")
    await device_spawner.spawn_camera("itest-sel-fnname-cam-2", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover

    await _wait_for_devices(
        messaging_url, {"itest-sel-fnname-cam-1", "itest-sel-fnname-cam-2"}
    )
    try:
        result = await asyncio.to_thread(
            discover, "device(*).function(capture_image)"
        )
        device_ids = {row["device_id"] for row in result["results"]}
        assert {"itest-sel-fnname-cam-1", "itest-sel-fnname-cam-2"} <= device_ids
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_function_by_direction_label(device_spawner, messaging_url):
    """``device(*).function(direction:write)`` matches on FunctionDef labels."""
    await device_spawner.spawn_camera("itest-sel-dir-cam", location="lab-A")
    await device_spawner.spawn_sensor("itest-sel-dir-sensor", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover

    await _wait_for_devices(messaging_url, {"itest-sel-dir-cam", "itest-sel-dir-sensor"})
    try:
        result = await asyncio.to_thread(
            discover, "device(*).function(direction:write)"
        )
        names = {row.get("name") for row in result["results"]}
        # camera.capture_image (write), sensor.set_threshold (write),
        # sensor.set_location (write)
        assert "capture_image" in names
        assert "set_threshold" in names
        assert "get_reading" not in names  # direction:read
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_function_safety_critical(device_spawner, messaging_url):
    """``function(safety:critical)`` returns critical RPCs fleet-wide."""
    await device_spawner.spawn_robot("itest-sel-crit-robot", location="lab-A")
    await device_spawner.spawn_sensor("itest-sel-crit-sensor", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover

    await _wait_for_devices(messaging_url, {"itest-sel-crit-robot", "itest-sel-crit-sensor"})
    try:
        result = await asyncio.to_thread(discover, "function(safety:critical)")
        assert result["scope"] == "function_only"
        names = {row.get("name") for row in result["results"]}
        # robot.dispatch_robot, sensor.set_threshold are safety:critical
        assert "dispatch_robot" in names
        assert "set_threshold" in names
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_function_and_labels(device_spawner, messaging_url):
    """``function(direction:write, modality:rgb)`` ANDs across function labels."""
    await device_spawner.spawn_camera("itest-sel-fnand-cam", location="lab-A")
    await device_spawner.spawn_sensor("itest-sel-fnand-sensor", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover

    await _wait_for_devices(messaging_url, {"itest-sel-fnand-cam", "itest-sel-fnand-sensor"})
    try:
        result = await asyncio.to_thread(
            discover, "function(direction:write, modality:rgb)"
        )
        names = {row.get("name") for row in result["results"]}
        # only camera.capture_image is direction:write AND modality:rgb
        assert names == {"capture_image"} or "capture_image" in names
        assert "set_threshold" not in names  # write but no modality:rgb
    finally:
        await asyncio.to_thread(disconnect)


# -- discover: event-scoped -----------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_event_by_name_fleet_wide(device_spawner, messaging_url):
    """``event(<name>)`` returns events fleet-wide."""
    await device_spawner.spawn_camera("itest-sel-evname-cam", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover

    await _wait_for_devices(messaging_url, {"itest-sel-evname-cam"})
    try:
        result = await asyncio.to_thread(discover, "event(object_detected)")
        assert result["scope"] == "event_only"
        device_ids = {row["device_id"] for row in result["results"]}
        assert "itest-sel-evname-cam" in device_ids
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_event_by_modality_label(device_spawner, messaging_url):
    """``device(*).event(modality:rgb)`` matches on EventDef labels."""
    await device_spawner.spawn_camera("itest-sel-evmod-cam", location="lab-A")
    await device_spawner.spawn_sensor("itest-sel-evmod-sensor", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover

    await _wait_for_devices(messaging_url, {"itest-sel-evmod-cam", "itest-sel-evmod-sensor"})
    try:
        result = await asyncio.to_thread(
            discover, "device(*).event(modality:rgb)"
        )
        names = {row.get("name") for row in result["results"]}
        # camera.object_detected has modality:rgb
        assert "object_detected" in names
        # sensor.reading has modality:thermal, not rgb
        assert "reading" not in names
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_event_safety_critical(device_spawner, messaging_url):
    """``event(safety:critical)`` finds the sensor.threshold_exceeded event."""
    await device_spawner.spawn_sensor("itest-sel-evcrit-sensor", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover

    await _wait_for_devices(messaging_url, {"itest-sel-evcrit-sensor"})
    try:
        result = await asyncio.to_thread(discover, "event(safety:critical)")
        names = {row.get("name") for row in result["results"]}
        assert "threshold_exceeded" in names
    finally:
        await asyncio.to_thread(disconnect)


# -- discover: pagination & errors ----------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_pagination(device_spawner, messaging_url):
    """``offset`` and ``limit`` produce stable, non-overlapping pages."""
    ids = {f"itest-sel-page-cam-{i}" for i in range(3)}
    for did in sorted(ids):
        await device_spawner.spawn_camera(did, location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover

    await _wait_for_devices(messaging_url, ids)
    try:
        page1 = await asyncio.to_thread(
            discover, "device(category:camera)", 0, 2
        )
        page2 = await asyncio.to_thread(
            discover, "device(category:camera)", page1["next_offset"] or 0, 2
        )
        assert page1["returned"] <= 2
        page1_ids = {d["device_id"] for d in page1["results"]}
        page2_ids = {d["device_id"] for d in page2["results"]}
        assert not (page1_ids & page2_ids), "pages should not overlap"
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_offset_past_end_returns_empty(device_spawner, messaging_url):
    """An offset beyond ``matched`` returns an empty page with ``next_offset=None``."""
    await device_spawner.spawn_camera("itest-sel-oob-cam", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover

    await _wait_for_devices(messaging_url, {"itest-sel-oob-cam"})
    try:
        result = await asyncio.to_thread(discover, "device(*)", 9999, 50)
        assert result["returned"] == 0
        assert result["results"] == []
        assert result["next_offset"] is None
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_invalid_selector_returns_error(device_spawner, messaging_url):
    """A bad selector returns an error-as-data envelope, not a raise."""
    await device_spawner.spawn_camera("itest-sel-err-cam", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, discover

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        result = await asyncio.to_thread(discover, "device(")
        assert result["error"]["code"] == "selector_parse_error"
        assert result["matched"] == 0
        assert result["results"] == []
    finally:
        await asyncio.to_thread(disconnect)


# -- discover_labels() -----------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_labels_includes_category(device_spawner, messaging_url):
    """Vocabulary surfaces ``category`` from device-level labels."""
    await device_spawner.spawn_camera("itest-sel-vcat-cam", location="lab-A")
    await device_spawner.spawn_robot("itest-sel-vcat-robot", location="lab-A")
    await device_spawner.spawn_sensor("itest-sel-vcat-sensor", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover_labels

    await _wait_for_devices(
        messaging_url,
        {"itest-sel-vcat-cam", "itest-sel-vcat-robot", "itest-sel-vcat-sensor"},
    )
    try:
        result = await asyncio.to_thread(discover_labels)
        cat = result["device_keys"].get("category")
        assert cat is not None
        values = cat["values"]
        assert "camera" in values
        assert "robot" in values
        assert "sensor" in values
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_labels_includes_location_via_mirror(
    device_spawner, messaging_url
):
    """Vocabulary surfaces ``location`` even when only ``DeviceStatus.location`` is set."""
    await device_spawner.spawn_camera("itest-sel-vloc-cam", location="lab-A")
    await device_spawner.spawn_sensor("itest-sel-vloc-sensor", location="lab-B")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover_labels

    await _wait_for_devices(messaging_url, {"itest-sel-vloc-cam", "itest-sel-vloc-sensor"})
    try:
        result = await asyncio.to_thread(discover_labels)
        loc = result["device_keys"].get("location")
        assert loc is not None
        values = loc["values"]
        assert "lab-A" in values
        assert "lab-B" in values
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_labels_function_direction_histogram(
    device_spawner, messaging_url
):
    """Function-axis vocabulary surfaces ``direction`` with read/write counts."""
    await device_spawner.spawn_camera("itest-sel-vdir-cam", location="lab-A")
    await device_spawner.spawn_sensor("itest-sel-vdir-sensor", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover_labels

    await _wait_for_devices(messaging_url, {"itest-sel-vdir-cam", "itest-sel-vdir-sensor"})
    try:
        result = await asyncio.to_thread(discover_labels)
        direction = result["function_keys"].get("direction")
        assert direction is not None
        values = direction["values"]
        assert "read" in values
        assert "write" in values
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_labels_per_key_pagination(device_spawner, messaging_url):
    """``discover_labels(key='device.category')`` paginates one key's values."""
    await device_spawner.spawn_camera("itest-sel-vpg-cam", location="lab-A")
    await device_spawner.spawn_robot("itest-sel-vpg-robot", location="lab-A")
    await device_spawner.spawn_sensor("itest-sel-vpg-sensor", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover_labels

    await _wait_for_devices(
        messaging_url,
        {"itest-sel-vpg-cam", "itest-sel-vpg-robot", "itest-sel-vpg-sensor"},
    )
    try:
        result = await asyncio.to_thread(discover_labels, "device.category")
        assert result["axis"] == "device"
        assert result["key"] == "category"
        assert "values" in result
        # at least camera, robot, sensor are present
        assert {"camera", "robot", "sensor"} <= set(result["values"].keys())
    finally:
        await asyncio.to_thread(disconnect)


# -- PR 28 review follow-ups: end-to-end regression guards ------------
#
# These tests cover behavior that was added or pinned in PR 28's review
# round: bracket character-class globs on the name axis, long-tail
# truncation in the multi-axis vocabulary form, case-sensitive selector
# matching, and the documented response shape contract.


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_bracket_glob_name_match(device_spawner, messaging_url):
    """``function([sg]et_*)`` end-to-end: bracket character class on the name
    axis routes through the matcher's fnmatch path and resolves against real
    device functions.

    Regression guard: before PR 28's heuristic fix, patterns containing only
    ``[``/``]`` (no ``*``/``?``) were treated as literal strings.
    """
    await device_spawner.spawn_sensor("itest-sel-bgg-sensor", location="lab-A")
    await device_spawner.spawn_robot("itest-sel-bgg-robot", location="lab-A")
    await device_spawner.spawn_camera("itest-sel-bgg-cam", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover

    await _wait_for_devices(
        messaging_url,
        {"itest-sel-bgg-sensor", "itest-sel-bgg-robot", "itest-sel-bgg-cam"},
    )
    try:
        # ``[sg]et_*`` matches sensor's set_threshold / set_location /
        # get_reading and robot's get_status; cam's capture_image and robot's
        # dispatch_robot start with neither ``s`` nor ``g`` + ``et_`` so are
        # rejected.
        result = await asyncio.to_thread(discover, "function([sg]et_*)")
        names = {row["name"] for row in result["results"]}
        assert {"set_threshold", "set_location", "get_reading", "get_status"} <= names
        assert "capture_image" not in names
        assert "dispatch_robot" not in names
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_labels_long_tail_truncates_multi_axis(
    device_spawner, messaging_url
):
    """Multi-axis ``discover_labels()`` crops each key's values to the top-N
    by frequency and reports the dropped count via a sibling ``more`` field.

    Patches ``LABEL_VALUES_TOP_N`` to a small value so 3 distinct locations
    are enough to trigger truncation; the real default is 20.
    """
    from unittest.mock import patch

    from device_connect_agent_tools import disconnect, discover_labels
    from device_connect_agent_tools import tools as tools_mod

    await device_spawner.spawn_sensor("itest-sel-ltl-s1", location="alpha")
    await device_spawner.spawn_sensor("itest-sel-ltl-s2", location="bravo")
    await device_spawner.spawn_sensor("itest-sel-ltl-s3", location="charlie")
    await asyncio.sleep(SETTLE_TIME)

    await _wait_for_devices(
        messaging_url,
        {"itest-sel-ltl-s1", "itest-sel-ltl-s2", "itest-sel-ltl-s3"},
    )
    try:
        with patch.object(tools_mod, "LABEL_VALUES_TOP_N", 2):
            result = await asyncio.to_thread(discover_labels)
        location = result["device_keys"]["location"]
        assert len(location["values"]) == 2
        # At least one location got dropped; expect >= 1 (other tests may add more).
        assert "more" in location, "truncation must emit the more field"
        assert location["more"] >= 1
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_labels_per_key_not_truncated(device_spawner, messaging_url):
    """Per-key ``discover_labels(key=...)`` uses pagination, not top-N
    truncation. Even with ``LABEL_VALUES_TOP_N`` set very low, every distinct
    value is reachable via the ``offset`` / ``next_offset`` cursor.
    """
    from unittest.mock import patch

    from device_connect_agent_tools import disconnect, discover_labels
    from device_connect_agent_tools import tools as tools_mod

    await device_spawner.spawn_sensor("itest-sel-pkn-s1", location="zone-1")
    await device_spawner.spawn_sensor("itest-sel-pkn-s2", location="zone-2")
    await device_spawner.spawn_sensor("itest-sel-pkn-s3", location="zone-3")
    await asyncio.sleep(SETTLE_TIME)

    await _wait_for_devices(
        messaging_url,
        {"itest-sel-pkn-s1", "itest-sel-pkn-s2", "itest-sel-pkn-s3"},
    )
    try:
        with patch.object(tools_mod, "LABEL_VALUES_TOP_N", 1):
            result = await asyncio.to_thread(
                discover_labels, "device.location"
            )
        assert "more" not in result
        # Per-key form returns the full distinct value list (subject to
        # default limit, which is well above 3 here).
        assert {"zone-1", "zone-2", "zone-3"} <= set(result["values"].keys())
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_label_histogram_truncates(device_spawner, messaging_url):
    """``discover()`` returns a ``label_histogram`` with the same per-key
    entry shape as ``discover_labels()``; long-tail truncation applies there
    too.
    """
    from unittest.mock import patch

    from device_connect_agent_tools import disconnect, discover
    from device_connect_agent_tools import tools as tools_mod

    await device_spawner.spawn_sensor("itest-sel-lht-s1", location="north")
    await device_spawner.spawn_sensor("itest-sel-lht-s2", location="south")
    await device_spawner.spawn_sensor("itest-sel-lht-s3", location="east")
    await asyncio.sleep(SETTLE_TIME)

    await _wait_for_devices(
        messaging_url,
        {"itest-sel-lht-s1", "itest-sel-lht-s2", "itest-sel-lht-s3"},
    )
    try:
        with patch.object(tools_mod, "LABEL_VALUES_TOP_N", 2):
            result = await asyncio.to_thread(discover, "device(*)")
        hist_location = result["label_histogram"]["location"]
        assert len(hist_location["values"]) == 2
        assert "more" in hist_location, "truncation must emit the more field"
        assert hist_location["more"] >= 1
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_case_sensitive_label_match(device_spawner, messaging_url):
    """Selector matching is case-sensitive on label values. ``Camera`` does
    not match ``camera``; the documented behavior is pinned here so a future
    accidental lowercasing change is caught.
    """
    await device_spawner.spawn_camera("itest-sel-cs-cam-1", location="lab-A")
    await device_spawner.spawn_camera("itest-sel-cs-cam-2", location="lab-A")
    await device_spawner.spawn_sensor("itest-sel-cs-sensor", location="lab-A")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover

    await _wait_for_devices(
        messaging_url,
        {"itest-sel-cs-cam-1", "itest-sel-cs-cam-2", "itest-sel-cs-sensor"},
    )
    try:
        lower = await asyncio.to_thread(discover, "device(category:camera)")
        upper = await asyncio.to_thread(discover, "device(category:Camera)")
        lower_ids = {row["device_id"] for row in lower["results"]}
        assert {"itest-sel-cs-cam-1", "itest-sel-cs-cam-2"} <= lower_ids
        assert upper["matched"] == 0
        assert upper["results"] == []
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_labels_response_shape_pinned(device_spawner, messaging_url):
    """Pins the response-envelope contract documented in ``docs/discovery.md``
    §"Response envelopes" against a real backend. Multi-axis form must carry
    ``total_devices`` / ``total_functions`` / ``total_events`` plus the three
    axis maps; per-key form must carry ``axis`` / ``key`` / ``matched`` /
    ``returned`` / ``offset`` / ``next_offset`` / ``values`` / ``axis_total``.
    """
    await device_spawner.spawn_camera("itest-sel-shape-cam", location="lab-A")
    await device_spawner.spawn_sensor("itest-sel-shape-sensor", location="lab-B")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import disconnect, discover_labels

    await _wait_for_devices(
        messaging_url,
        {"itest-sel-shape-cam", "itest-sel-shape-sensor"},
    )
    try:
        multi = await asyncio.to_thread(discover_labels)
        for field in ("total_devices", "total_functions", "total_events",
                      "device_keys", "function_keys", "event_keys"):
            assert field in multi, f"multi-axis response missing {field!r}"

        per_key = await asyncio.to_thread(discover_labels, "device.location")
        for field in ("axis", "key", "matched", "returned", "offset",
                      "next_offset", "values", "axis_total"):
            assert field in per_key, f"per-key response missing {field!r}"
        # ``multivalued`` is conditional; document the contract by checking it
        # is absent on a single-valued key (location).
        assert "multivalued" not in per_key
    finally:
        await asyncio.to_thread(disconnect)
