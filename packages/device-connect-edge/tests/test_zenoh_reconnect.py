# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for ZenohAdapter router-restart resilience.

Covers the persistent-reconnect config and the session-health watchdog that
re-opens the session and re-declares all subscriptions/queryables after a hard
session close (e.g. a router restart triggered by a tenant-ACL change). All
Zenoh SDK internals are mocked -- no real session is needed.
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest


def _make_mock_session(closed: bool = False):
    session = MagicMock()
    session.is_closed = MagicMock(return_value=closed)
    session.put = MagicMock()
    session.get = MagicMock(return_value=[])
    session.declare_subscriber = MagicMock(return_value=MagicMock())
    session.declare_queryable = MagicMock(return_value=MagicMock())
    session.close = MagicMock()
    return session


@pytest.mark.asyncio
@patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
@patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
async def test_connect_sets_persistent_retry_config(mock_zenoh):
    """A router-bound client must keep retrying instead of exiting on failure."""
    mock_zenoh.open = MagicMock(return_value=_make_mock_session())
    mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

    from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

    adapter = ZenohAdapter()
    await adapter.connect(servers=["tls/router:7447"])

    cfg = json.loads(mock_zenoh.Config.from_json5.call_args[0][0])
    assert cfg["connect"]["exit_on_failure"] is False
    assert "retry" in cfg["connect"]
    await adapter.close()


@pytest.mark.asyncio
@patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
@patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
async def test_connect_starts_watchdog(mock_zenoh):
    mock_zenoh.open = MagicMock(return_value=_make_mock_session())
    mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

    from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

    adapter = ZenohAdapter()
    await adapter.connect(servers=["tls/router:7447"])
    assert adapter._watchdog_task is not None
    assert not adapter._watchdog_task.done()
    await adapter.close()
    assert adapter._watchdog_task is None


@pytest.mark.asyncio
@patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
@patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
async def test_watchdog_redeclares_on_session_close(mock_zenoh):
    """A hard session close triggers a re-open and replays subscriptions."""
    session1 = _make_mock_session()
    session2 = _make_mock_session()
    mock_zenoh.open = MagicMock(side_effect=[session1, session2])
    mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

    from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

    adapter = ZenohAdapter()
    adapter._watchdog_interval = 0.05  # speed up the test
    await adapter.connect(servers=["tls/router:7447"])

    received: list = []

    async def cb(data, reply):
        received.append(data)

    wrapper = await adapter.subscribe("device-connect.t.dev.cmd", cb)
    assert session1.declare_subscriber.call_count == 1
    assert session1.declare_queryable.call_count == 1

    # Simulate the router restart: the old session reports closed.
    session1.is_closed.return_value = True

    # Wait for the watchdog to detect the close and reconnect.
    for _ in range(40):
        await asyncio.sleep(0.05)
        if adapter._session is session2:
            break

    assert adapter._session is session2, "watchdog did not re-open the session"
    assert adapter.is_connected is True
    # Subscriptions were replayed onto the new session.
    assert session2.declare_subscriber.call_count == 1
    assert session2.declare_queryable.call_count == 1
    # The caller's Subscription handle was updated in place, not orphaned.
    assert wrapper._subscriber is session2.declare_subscriber.return_value
    assert "device-connect/t/dev/cmd" in adapter._subscriptions

    await adapter.close()


@pytest.mark.asyncio
@patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
@patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
async def test_close_stops_watchdog_no_reconnect(mock_zenoh):
    """After close(), a closed session must NOT trigger a reconnect."""
    session1 = _make_mock_session()
    mock_zenoh.open = MagicMock(side_effect=[session1, _make_mock_session()])
    mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

    from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

    adapter = ZenohAdapter()
    adapter._watchdog_interval = 0.05
    await adapter.connect(servers=["tls/router:7447"])
    await adapter.close()

    open_calls = mock_zenoh.open.call_count
    session1.is_closed.return_value = True
    await asyncio.sleep(0.2)
    # No new session opened after close.
    assert mock_zenoh.open.call_count == open_calls
