# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the portal NATS RPC helper.

Focused on the cached-client behavior introduced when the portal stopped
opening a fresh connection per invoke():

- a single cached client is reused across calls
- transport errors drop the cached client so the next call reconnects
- non-transport errors (payload bugs, ProtocolError, ...) do NOT churn
  the cached connection
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import nats.errors
import pytest

from device_connect_server.portal.services import nats_rpc


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset the module-level cached client + lock between tests.

    The cached client is module state — without this, tests would leak
    fakes into each other and the lock would bind to a stale event loop.
    """
    nats_rpc._invoke_client = None
    nats_rpc._invoke_client_lock = None
    yield
    nats_rpc._invoke_client = None
    nats_rpc._invoke_client_lock = None


def _make_fake_nc(*, request_reply: bytes | None = None, request_exc: Exception | None = None):
    """Build a fake nats client that returns request_reply or raises request_exc."""
    nc = MagicMock()
    nc.is_closed = False
    if request_exc is not None:
        nc.request = AsyncMock(side_effect=request_exc)
    else:
        msg = MagicMock()
        msg.data = request_reply or b'{"jsonrpc":"2.0","id":"x","result":{}}'
        nc.request = AsyncMock(return_value=msg)
    nc.close = AsyncMock()
    return nc


class TestInvokeClientReuse:

    @pytest.mark.asyncio
    async def test_client_is_reused_across_calls(self):
        """Two invoke() calls share the same underlying NATS client."""
        fake_nc = _make_fake_nc(
            request_reply=b'{"jsonrpc":"2.0","id":"1","result":{"ok":true}}',
        )

        with patch.object(nats_rpc, "connect", AsyncMock(return_value=fake_nc)) as connect_mock:
            r1 = await nats_rpc.invoke("t", "dev-1", "fn", {})
            r2 = await nats_rpc.invoke("t", "dev-2", "fn", {})

        # Single connect() call shared across both invokes.
        assert connect_mock.await_count == 1
        # Both requests went through the same client.
        assert fake_nc.request.await_count == 2
        assert r1["result"]["ok"] is True
        assert r2["result"]["ok"] is True

    @pytest.mark.asyncio
    async def test_client_is_reconnected_if_closed(self):
        """A cached-but-closed client triggers a fresh connect on next call."""
        first_nc = _make_fake_nc(
            request_reply=b'{"jsonrpc":"2.0","id":"1","result":{}}',
        )
        second_nc = _make_fake_nc(
            request_reply=b'{"jsonrpc":"2.0","id":"2","result":{}}',
        )

        with patch.object(
            nats_rpc, "connect", AsyncMock(side_effect=[first_nc, second_nc]),
        ) as connect_mock:
            await nats_rpc.invoke("t", "dev-1", "fn", {})
            # Simulate the broker closing the connection out from under us.
            first_nc.is_closed = True
            await nats_rpc.invoke("t", "dev-2", "fn", {})

        assert connect_mock.await_count == 2


class TestInvokeClientDropOnTransportError:

    @pytest.mark.asyncio
    async def test_transport_error_drops_cached_client(self):
        """ConnectionClosedError forces a reconnect on the next call."""
        bad_nc = _make_fake_nc(
            request_exc=nats.errors.ConnectionClosedError(),
        )
        good_nc = _make_fake_nc(
            request_reply=b'{"jsonrpc":"2.0","id":"2","result":{"ok":true}}',
        )

        with patch.object(
            nats_rpc, "connect", AsyncMock(side_effect=[bad_nc, good_nc]),
        ) as connect_mock:
            err = await nats_rpc.invoke("t", "dev-1", "fn", {})
            ok = await nats_rpc.invoke("t", "dev-2", "fn", {})

        assert err["error"]["code"] == -3
        assert ok["result"]["ok"] is True
        # First connect for bad_nc, second connect after drop.
        assert connect_mock.await_count == 2
        # The bad client was best-effort closed when dropped.
        bad_nc.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stale_connection_error_drops_cached_client(self):
        """StaleConnectionError is also treated as transport-fatal."""
        bad_nc = _make_fake_nc(
            request_exc=nats.errors.StaleConnectionError(),
        )
        good_nc = _make_fake_nc(
            request_reply=b'{"jsonrpc":"2.0","id":"2","result":{}}',
        )

        with patch.object(
            nats_rpc, "connect", AsyncMock(side_effect=[bad_nc, good_nc]),
        ) as connect_mock:
            await nats_rpc.invoke("t", "dev-1", "fn", {})
            await nats_rpc.invoke("t", "dev-2", "fn", {})

        assert connect_mock.await_count == 2

    @pytest.mark.asyncio
    async def test_os_error_drops_cached_client(self):
        """Raw OSError (socket-level) is treated as transport-fatal too."""
        bad_nc = _make_fake_nc(
            request_exc=OSError("socket reset"),
        )
        good_nc = _make_fake_nc(
            request_reply=b'{"jsonrpc":"2.0","id":"2","result":{}}',
        )

        with patch.object(
            nats_rpc, "connect", AsyncMock(side_effect=[bad_nc, good_nc]),
        ) as connect_mock:
            await nats_rpc.invoke("t", "dev-1", "fn", {})
            await nats_rpc.invoke("t", "dev-2", "fn", {})

        assert connect_mock.await_count == 2


class TestInvokeClientKeptOnNonTransportError:

    @pytest.mark.asyncio
    async def test_protocol_error_keeps_cached_client(self):
        """ProtocolError is a payload bug, not a connection death.

        Dropping the client on every payload error would churn the
        connection — exactly the regression we wanted to avoid by
        narrowing the exception handler. The cached client must survive.
        """
        nc = MagicMock()
        nc.is_closed = False
        good_reply = MagicMock()
        good_reply.data = b'{"jsonrpc":"2.0","id":"2","result":{"ok":true}}'
        nc.request = AsyncMock(
            side_effect=[nats.errors.ProtocolError(), good_reply],
        )
        nc.close = AsyncMock()

        with patch.object(
            nats_rpc, "connect", AsyncMock(return_value=nc),
        ) as connect_mock:
            err = await nats_rpc.invoke("t", "dev-1", "fn", {})
            ok = await nats_rpc.invoke("t", "dev-1", "fn", {})

        assert err["error"]["code"] == -3
        assert ok["result"]["ok"] is True
        # Single connect, no drop: the cached client was reused across
        # the protocol error.
        assert connect_mock.await_count == 1
        nc.close.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_responders_keeps_cached_client(self):
        """NoRespondersError already has its own branch and never drops."""
        nc = MagicMock()
        nc.is_closed = False
        good_reply = MagicMock()
        good_reply.data = b'{"jsonrpc":"2.0","id":"2","result":{"ok":true}}'
        nc.request = AsyncMock(
            side_effect=[nats.errors.NoRespondersError(), good_reply],
        )
        nc.close = AsyncMock()

        with patch.object(
            nats_rpc, "connect", AsyncMock(return_value=nc),
        ) as connect_mock:
            no_resp = await nats_rpc.invoke("t", "dev-1", "fn", {})
            ok = await nats_rpc.invoke("t", "dev-1", "fn", {})

        assert no_resp["error"]["code"] == -1
        assert ok["result"]["ok"] is True
        assert connect_mock.await_count == 1
        nc.close.assert_not_called()
