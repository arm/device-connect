# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Shared helpers for slow scale integration tests."""

import asyncio
import os
import time
from collections.abc import Callable, Iterable

DEFAULT_SCALE_FLEET_SIZE = 200


def scale_fleet_size(*, minimum: int = 1, default: int = DEFAULT_SCALE_FLEET_SIZE) -> int:
    return max(minimum, int(os.getenv("DC_SCALE_FLEET_SIZE", str(default))))


async def wait_for_devices(
    messaging_url: str,
    expected_ids: Iterable[str],
    *,
    timeout: float,
    invalidate_cache: bool = False,
) -> list[dict]:
    """Connect and poll until all expected device IDs are visible."""
    from device_connect_agent_tools import connect
    from device_connect_agent_tools.connection import get_connection

    expected = set(expected_ids)
    await asyncio.to_thread(connect, nats_url=messaging_url)
    deadline = time.monotonic() + timeout
    while True:
        conn = get_connection()
        if invalidate_cache:
            conn.invalidate_cache()
        devices = await asyncio.to_thread(conn.list_devices)
        ids = {d.get("device_id") for d in devices}
        if expected <= ids or time.monotonic() > deadline:
            return devices
        await asyncio.sleep(0.25)


def assert_compact_function_rows(rows: list[dict]) -> None:
    assert rows
    for row in rows:
        assert set(row) <= {"device_id", "name", "labels"}
        assert "parameters" not in row
        assert "description" not in row


def assert_expanded_function_rows(rows: list[dict]) -> None:
    assert rows
    for row in rows:
        assert "device_id" in row
        assert "name" in row
        assert "parameters" in row
        assert "description" in row


def assert_device_row_compact(row: dict) -> None:
    assert "function_count" in row
    assert "function_names" in row
    assert "functions" not in row
    assert "events" not in row
    assert "capabilities" not in row


def assert_device_row_expanded(row: dict, expected_function: str) -> None:
    assert "function_count" in row
    assert "function_names" in row
    assert "functions" in row
    function = next(
        (fn for fn in row["functions"] if fn["name"] == expected_function),
        None,
    )
    assert function is not None
    assert "parameters" in function


async def spawn_scale_sensor_fleet(
    device_spawner,
    *,
    prefix: str,
    fleet_size: int,
    settle_time: float,
    location: str = "scale-room",
    location_for: Callable[[int], str] | None = None,
    initial_temp: float = 21.0,
    registration_timeout: float = 30.0,
) -> set[str]:
    expected_ids = {f"{prefix}-{i:04d}" for i in range(fleet_size)}
    await device_spawner.spawn_sensor_fleet(
        prefix,
        fleet_size,
        location=location,
        location_for=location_for,
        initial_temp=initial_temp,
        registration_timeout=registration_timeout,
    )
    await asyncio.sleep(settle_time)
    return expected_ids
