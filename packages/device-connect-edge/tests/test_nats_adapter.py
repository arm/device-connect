# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for device_connect_edge.messaging.nats_adapter — NATSAdapter.

All NATS client internals are mocked so no real NATS server is needed.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

# ---------------------------------------------------------------------------
# We mock the nats module at import-time so the adapter never touches a real
# NATS client.  Every test patches "device_connect_edge.messaging.nats_adapter.NATS".
# ---------------------------------------------------------------------------


def _make_mock_nats(connected: bool = True, closed: bool = False):
    """Return a mock NATS client with sane defaults."""
    mock_nats = AsyncMock()
    type(mock_nats).is_connected = PropertyMock(return_value=connected)
    type(mock_nats).is_closed = PropertyMock(return_value=closed)
    mock_nats.publish = AsyncMock()
    mock_nats.subscribe = AsyncMock(return_value=AsyncMock())  # subscription object
    mock_nats.request = AsyncMock(
        return_value=SimpleNamespace(data=b'{"ok": true}')
    )
    mock_nats.close = AsyncMock()
    mock_nats.drain = AsyncMock()
    mock_nats.connect = AsyncMock()
    return mock_nats


# ---------------------------------------------------------------------------
# TestNATSClientInit
# ---------------------------------------------------------------------------


class TestNATSClientInit:
    """Constructor and default attribute values."""

    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    def test_constructor_creates_nats_client(self, MockNATS):
        MockNATS.return_value = _make_mock_nats()
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        assert adapter._nc is not None

    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    def test_default_nkey_seed_is_none(self, MockNATS):
        MockNATS.return_value = _make_mock_nats()
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        assert adapter._nkey_seed is None

    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    def test_default_reconnecting_false(self, MockNATS):
        MockNATS.return_value = _make_mock_nats()
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        assert adapter._reconnecting is False

    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    def test_default_ever_connected_false(self, MockNATS):
        MockNATS.return_value = _make_mock_nats()
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        assert adapter._ever_connected is False


# ---------------------------------------------------------------------------
# TestNATSClientConnect
# ---------------------------------------------------------------------------


class TestNATSClientConnect:
    """Verify connect() passes servers, credentials, and TLS correctly."""

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_connect_passes_servers(self, MockNATS):
        mock_nats= _make_mock_nats()
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        await adapter.connect(servers=["nats://host1:4222", "nats://host2:4222"])

        mock_nats.connect.assert_awaited_once()
        call_kwargs = mock_nats.connect.call_args[1]
        assert call_kwargs["servers"] == ["nats://host1:4222", "nats://host2:4222"]

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_connect_default_reconnect_options(self, MockNATS):
        mock_nats= _make_mock_nats()
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        await adapter.connect(servers=["nats://localhost:4222"])

        call_kwargs = mock_nats.connect.call_args[1]
        assert call_kwargs["reconnect_time_wait"] == 2
        assert call_kwargs["max_reconnect_attempts"] == -1

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_connect_sets_ever_connected(self, MockNATS):
        mock_nats= _make_mock_nats()
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        await adapter.connect(servers=["nats://localhost:4222"])
        assert adapter._ever_connected is True

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_connect_with_jwt_credentials(self, MockNATS):
        mock_nats= _make_mock_nats()
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        creds = {"jwt": "my.jwt.token", "nkey_seed": "SUAIBDPBAUT"}
        await adapter.connect(servers=["nats://localhost:4222"], credentials=creds)

        call_kwargs = mock_nats.connect.call_args[1]
        assert "user_jwt_cb" in call_kwargs
        assert "signature_cb" in call_kwargs
        assert adapter._nkey_seed == "SUAIBDPBAUT"

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_connect_jwt_uses_custom_signature_cb(self, MockNATS):
        mock_nats= _make_mock_nats()
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        custom_cb = MagicMock()
        creds = {"jwt": "my.jwt.token", "nkey_seed": "SEED", "signature_cb": custom_cb}
        adapter = NATSAdapter()
        await adapter.connect(servers=["nats://localhost:4222"], credentials=creds)

        call_kwargs = mock_nats.connect.call_args[1]
        assert call_kwargs["signature_cb"] is custom_cb

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_connect_partial_credentials_warns(self, MockNATS):
        """If only jwt OR nkey_seed is supplied, adapter logs warning and skips auth."""
        mock_nats= _make_mock_nats()
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        await adapter.connect(
            servers=["nats://localhost:4222"],
            credentials={"jwt": "token-only"},
        )

        call_kwargs = mock_nats.connect.call_args[1]
        assert "user_jwt_cb" not in call_kwargs

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_connect_with_tls_config(self, MockNATS):
        mock_nats= _make_mock_nats()
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        with patch.object(adapter, "_build_tls_context", return_value="fake_ctx") as mock_tls:
            await adapter.connect(
                servers=["nats://localhost:4222"],
                tls_config={"ca_file": "/tmp/ca.pem"},
            )
            mock_tls.assert_called_once_with({"ca_file": "/tmp/ca.pem"})
            call_kwargs = mock_nats.connect.call_args[1]
            assert call_kwargs["tls"] == "fake_ctx"

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_connect_strips_dc_only_keys(self, MockNATS):
        mock_nats= _make_mock_nats()
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        await adapter.connect(
            servers=["nats://localhost:4222"],
            allow_insecure=True,
        )

        call_kwargs = mock_nats.connect.call_args[1]
        assert "allow_insecure" not in call_kwargs

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_connect_failure_raises_connection_error(self, MockNATS):
        mock_nats= _make_mock_nats()
        mock_nats.connect = AsyncMock(side_effect=Exception("refused"))
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter
        from device_connect_edge.messaging.exceptions import MessagingConnectionError

        adapter = NATSAdapter()
        with pytest.raises(MessagingConnectionError, match="refused"):
            await adapter.connect(servers=["nats://localhost:4222"])

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_connect_auth_failure_raises_authentication_error(self, MockNATS):
        mock_nats= _make_mock_nats()
        mock_nats.connect = AsyncMock(side_effect=Exception("authorization violation"))
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter
        from device_connect_edge.messaging.exceptions import AuthenticationError

        adapter = NATSAdapter()
        with pytest.raises(AuthenticationError, match="Authentication failed"):
            await adapter.connect(servers=["nats://localhost:4222"])


# ---------------------------------------------------------------------------
# TestNATSClientPublish
# ---------------------------------------------------------------------------


class TestNATSClientPublish:
    """Verify publish() delegates to mock_nats.publish with correct args."""

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_publish_calls_nc_publish(self, MockNATS):
        mock_nats= _make_mock_nats(connected=True)
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        await adapter.publish("device.event.started", b'{"status":"ok"}')

        mock_nats.publish.assert_awaited_once_with("device.event.started", b'{"status":"ok"}')

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_publish_when_disconnected_raises(self, MockNATS):
        mock_nats= _make_mock_nats(connected=False)
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter
        from device_connect_edge.messaging.exceptions import NotConnectedError

        adapter = NATSAdapter()
        with pytest.raises(NotConnectedError):
            await adapter.publish("subject", b"data")

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_publish_error_raises_publish_error(self, MockNATS):
        mock_nats= _make_mock_nats(connected=True)
        mock_nats.publish = AsyncMock(side_effect=Exception("flush failed"))
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter
        from device_connect_edge.messaging.exceptions import PublishError

        adapter = NATSAdapter()
        with pytest.raises(PublishError, match="flush failed"):
            await adapter.publish("subject", b"data")


# ---------------------------------------------------------------------------
# TestNATSClientSubscribe
# ---------------------------------------------------------------------------


class TestNATSClientSubscribe:
    """Verify subscribe() wires callback correctly."""

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_subscribe_calls_nc_subscribe(self, MockNATS):
        mock_nats= _make_mock_nats(connected=True)
        mock_sub = AsyncMock()
        mock_nats.subscribe = AsyncMock(return_value=mock_sub)
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        callback = AsyncMock()
        adapter = NATSAdapter()
        sub = await adapter.subscribe("events.>", callback)

        mock_nats.subscribe.assert_awaited_once()
        call_args = mock_nats.subscribe.call_args
        assert call_args[0][0] == "events.>"
        assert sub is not None

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_subscribe_with_queue_group(self, MockNATS):
        mock_nats= _make_mock_nats(connected=True)
        mock_nats.subscribe = AsyncMock(return_value=AsyncMock())
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        await adapter.subscribe("events.>", AsyncMock(), queue="workers")

        call_kwargs = mock_nats.subscribe.call_args[1]
        assert call_kwargs["queue"] == "workers"

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_subscribe_callback_wrapper_extracts_data_and_reply(self, MockNATS):
        mock_nats= _make_mock_nats(connected=True)
        captured_wrapper = None

        async def capture_subscribe(subject, cb=None, **kw):
            nonlocal captured_wrapper
            captured_wrapper = cb
            return AsyncMock()

        mock_nats.subscribe = AsyncMock(side_effect=capture_subscribe)
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        user_cb = AsyncMock()
        adapter = NATSAdapter()
        await adapter.subscribe("subject", user_cb)

        # Simulate NATS delivering a message
        fake_msg = SimpleNamespace(data=b"hello", reply="_INBOX.123")
        await captured_wrapper(fake_msg)

        user_cb.assert_awaited_once_with(b"hello", "_INBOX.123")

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_subscribe_when_disconnected_raises(self, MockNATS):
        mock_nats= _make_mock_nats(connected=False)
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter
        from device_connect_edge.messaging.exceptions import NotConnectedError

        adapter = NATSAdapter()
        with pytest.raises(NotConnectedError):
            await adapter.subscribe("subject", AsyncMock())

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_subscribe_error_raises_subscribe_error(self, MockNATS):
        mock_nats= _make_mock_nats(connected=True)
        mock_nats.subscribe = AsyncMock(side_effect=Exception("bad subject"))
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter
        from device_connect_edge.messaging.exceptions import SubscribeError

        adapter = NATSAdapter()
        with pytest.raises(SubscribeError, match="bad subject"):
            await adapter.subscribe("subject", AsyncMock())

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_unsubscribe_delegates_to_nats(self, MockNATS):
        mock_nats= _make_mock_nats(connected=True)
        mock_nats_sub = AsyncMock()
        mock_nats.subscribe = AsyncMock(return_value=mock_nats_sub)
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        sub = await adapter.subscribe("events.>", AsyncMock())
        await sub.unsubscribe()

        mock_nats_sub.unsubscribe.assert_awaited_once()


# ---------------------------------------------------------------------------
# TestNATSClientRequest
# ---------------------------------------------------------------------------


class TestNATSClientRequest:
    """Verify request() round-trip and error handling."""

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_request_returns_reply_data(self, MockNATS):
        mock_nats= _make_mock_nats(connected=True)
        mock_nats.request = AsyncMock(return_value=SimpleNamespace(data=b"reply-payload"))
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        result = await adapter.request("service.method", b"request-data")

        assert result == b"reply-payload"
        mock_nats.request.assert_awaited_once_with("service.method", b"request-data", timeout=5.0)

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_request_custom_timeout(self, MockNATS):
        mock_nats= _make_mock_nats(connected=True)
        mock_nats.request = AsyncMock(return_value=SimpleNamespace(data=b"ok"))
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        await adapter.request("service.method", b"data", timeout=10.0)

        mock_nats.request.assert_awaited_once_with("service.method", b"data", timeout=10.0)

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_request_timeout_raises(self, MockNATS):
        mock_nats= _make_mock_nats(connected=True)
        mock_nats.request = AsyncMock(side_effect=asyncio.TimeoutError())
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter
        from device_connect_edge.messaging.exceptions import RequestTimeoutError

        adapter = NATSAdapter()
        with pytest.raises(RequestTimeoutError, match="timed out"):
            await adapter.request("service.method", b"data", timeout=1.0)

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_request_when_disconnected_raises(self, MockNATS):
        mock_nats= _make_mock_nats(connected=False)
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter
        from device_connect_edge.messaging.exceptions import NotConnectedError

        adapter = NATSAdapter()
        with pytest.raises(NotConnectedError):
            await adapter.request("subject", b"data")

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_request_generic_error_raises_publish_error(self, MockNATS):
        mock_nats= _make_mock_nats(connected=True)
        mock_nats.request = AsyncMock(side_effect=Exception("network error"))
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter
        from device_connect_edge.messaging.exceptions import PublishError

        adapter = NATSAdapter()
        with pytest.raises(PublishError, match="network error"):
            await adapter.request("subject", b"data")


# ---------------------------------------------------------------------------
# TestNATSClientClose
# ---------------------------------------------------------------------------


class TestNATSClientClose:
    """Verify close() and drain() delegation."""

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_close_calls_nc_close(self, MockNATS):
        mock_nats= _make_mock_nats(connected=True, closed=False)
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        await adapter.close()

        mock_nats.close.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_close_skips_if_already_closed(self, MockNATS):
        mock_nats= _make_mock_nats(connected=False, closed=True)
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        await adapter.close()

        mock_nats.close.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_drain_calls_nc_drain(self, MockNATS):
        mock_nats= _make_mock_nats(connected=True, closed=False)
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        await adapter.drain()

        mock_nats.drain.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_drain_skips_if_closed(self, MockNATS):
        mock_nats= _make_mock_nats(connected=False, closed=True)
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        await adapter.drain()

        mock_nats.drain.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    async def test_disconnect_aliases_close(self, MockNATS):
        mock_nats= _make_mock_nats(connected=True, closed=False)
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        await adapter.disconnect()

        mock_nats.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# TestNATSClientProperties
# ---------------------------------------------------------------------------


class TestNATSClientProperties:
    """Verify is_connected and is_closed property delegation."""

    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    def test_is_connected_delegates(self, MockNATS):
        mock_nats= _make_mock_nats(connected=True)
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        assert adapter.is_connected is True

    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    def test_is_connected_false_when_disconnected(self, MockNATS):
        mock_nats= _make_mock_nats(connected=False)
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        assert adapter.is_connected is False

    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    def test_is_closed_delegates(self, MockNATS):
        mock_nats= _make_mock_nats(connected=False, closed=True)
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        assert adapter.is_closed is True

    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    def test_is_closed_false_when_open(self, MockNATS):
        mock_nats= _make_mock_nats(connected=True, closed=False)
        MockNATS.return_value = mock_nats
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        assert adapter.is_closed is False

    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    def test_is_connected_false_when_nc_is_none(self, MockNATS):
        MockNATS.return_value = _make_mock_nats()
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        adapter._nc = None
        assert adapter.is_connected is False

    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    def test_is_closed_true_when_nc_is_none(self, MockNATS):
        MockNATS.return_value = _make_mock_nats()
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        adapter._nc = None
        assert adapter.is_closed is True

    @patch("device_connect_edge.messaging.nats_adapter.NATS")
    def test_convert_subject_syntax_passthrough(self, MockNATS):
        MockNATS.return_value = _make_mock_nats()
        from device_connect_edge.messaging.nats_adapter import NATSAdapter

        adapter = NATSAdapter()
        assert adapter.convert_subject_syntax("a.b.c") == "a.b.c"
