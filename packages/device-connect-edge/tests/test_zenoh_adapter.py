# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for device_connect_edge.messaging.zenoh_adapter — ZenohAdapter.

All Zenoh SDK internals are mocked so no real Zenoh session is needed.
The Zenoh Python SDK is synchronous, so mocks use MagicMock (not AsyncMock).
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Mock helpers — simulate Zenoh SDK objects
# ---------------------------------------------------------------------------


def _make_mock_session(closed: bool = False):
    """Return a mock Zenoh session with sane defaults."""
    session = MagicMock()
    session.is_closed = MagicMock(return_value=closed)
    session.put = MagicMock()
    session.get = MagicMock(return_value=[])
    session.declare_subscriber = MagicMock(return_value=MagicMock())
    session.declare_queryable = MagicMock(return_value=MagicMock())
    session.close = MagicMock()
    session.zid = MagicMock(return_value="mock-zid-123")
    return session


def _make_mock_reply(payload: bytes):
    """Create a mock Reply with .ok.payload."""
    reply = MagicMock()
    reply.ok = MagicMock()
    reply.ok.payload = payload
    reply.err = None
    return reply


def _make_mock_reply_err(payload: bytes):
    """Create a mock error Reply with .err.payload."""
    reply = MagicMock()
    reply.ok = None
    reply.err = MagicMock()
    reply.err.payload = payload
    return reply


def _make_mock_sample(key_expr: str, payload: bytes):
    """Create a mock Zenoh Sample."""
    sample = MagicMock()
    sample.key_expr = key_expr
    sample.payload = payload
    return sample


def _make_mock_query(key_expr: str, payload: bytes = b""):
    """Create a mock Zenoh Query."""
    query = MagicMock()
    query.key_expr = key_expr
    query.payload = payload
    query.reply = MagicMock()
    return query


# ---------------------------------------------------------------------------
# TestZenohClientInit
# ---------------------------------------------------------------------------


class TestZenohClientInit:
    """Constructor and default attribute values."""

    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    def test_constructor_defaults(self, mock_zenoh):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        assert adapter._session is None
        assert adapter._connected is False
        assert adapter._closed is False

    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", False)
    def test_constructor_raises_when_zenoh_missing(self):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        with pytest.raises(ImportError, match="eclipse-zenoh"):
            ZenohAdapter()

    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    def test_default_is_connected_false(self, mock_zenoh):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        assert adapter.is_connected is False

    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    def test_default_is_closed_false(self, mock_zenoh):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        assert adapter.is_closed is False

    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    def test_executor_created(self, mock_zenoh):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        assert adapter._executor is not None


# ---------------------------------------------------------------------------
# TestZenohClientConnect
# ---------------------------------------------------------------------------


class TestZenohClientConnect:
    """Verify connect() handles URLs, peer mode, and TLS correctly."""

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_connect_opens_session(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/localhost:7447"])

        assert adapter.is_connected is True
        assert adapter._session is session
        mock_zenoh.open.assert_called_once()

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_connect_parses_zenoh_scheme(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["zenoh://myhost:7447"])

        call_args = mock_zenoh.Config.from_json5.call_args[0][0]
        config_dict = json.loads(call_args)
        assert "tcp/myhost:7447" in config_dict["connect"]["endpoints"]

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_connect_parses_zenoh_tls_scheme(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["zenoh+tls://secure:7447"])

        call_args = mock_zenoh.Config.from_json5.call_args[0][0]
        config_dict = json.loads(call_args)
        assert "tls/secure:7447" in config_dict["connect"]["endpoints"]

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_connect_peer_mode_enables_scouting(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["zenoh://"])

        call_args = mock_zenoh.Config.from_json5.call_args[0][0]
        config_dict = json.loads(call_args)
        assert config_dict["scouting"]["multicast"]["enabled"] is True
        assert config_dict["scouting"]["gossip"]["enabled"] is True
        assert config_dict["scouting"]["gossip"]["multihop"] is False
        # Verify d2d_mode flag is set
        assert adapter._d2d_mode is True

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_connect_router_mode_disables_scouting(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["zenoh://router:7447"])

        call_args = mock_zenoh.Config.from_json5.call_args[0][0]
        config_dict = json.loads(call_args)
        assert config_dict["scouting"]["multicast"]["enabled"] is False

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_connect_with_tls_config(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(
            servers=["tls/router:7447"],
            tls_config={
                "ca_file": "/certs/ca.pem",
                "cert_file": "/certs/device.pem",
                "key_file": "/certs/device.key",
            },
        )

        call_args = mock_zenoh.Config.from_json5.call_args[0][0]
        config_dict = json.loads(call_args)
        tls = config_dict["transport"]["link"]["tls"]
        assert tls["root_ca_certificate"] == "/certs/ca.pem"
        assert tls["client_certificate"] == "/certs/device.pem"
        assert tls["client_private_key"] == "/certs/device.key"

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_connect_sets_connected_true(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        assert adapter.is_connected is False
        await adapter.connect(servers=["tcp/host:7447"])
        assert adapter.is_connected is True

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_connect_failure_raises_connection_error(self, mock_zenoh):
        mock_zenoh.open = MagicMock(side_effect=Exception("Connection refused"))
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter
        from device_connect_edge.messaging.exceptions import MessagingConnectionError

        adapter = ZenohAdapter()
        with pytest.raises(MessagingConnectionError, match="Connection refused"):
            await adapter.connect(servers=["tcp/host:7447"])


# ---------------------------------------------------------------------------
# TestZenohClientPublish
# ---------------------------------------------------------------------------


class TestZenohClientPublish:
    """Verify publish() delegates to session.put()."""

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_publish_calls_session_put(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])
        await adapter.publish("test.subject", b"hello")

        session.put.assert_called_once_with("test/subject", b"hello")

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_publish_converts_subject_syntax(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])
        await adapter.publish("device-connect.default.cam-001.event.alert", b"data")

        session.put.assert_called_once_with(
            "device-connect/default/cam-001/event/alert", b"data"
        )

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_publish_when_disconnected_raises(self, mock_zenoh):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter
        from device_connect_edge.messaging.exceptions import NotConnectedError

        adapter = ZenohAdapter()
        with pytest.raises(NotConnectedError):
            await adapter.publish("test.subject", b"hello")

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_publish_error_raises_publish_error(self, mock_zenoh):
        session = _make_mock_session()
        session.put = MagicMock(side_effect=Exception("put failed"))
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter
        from device_connect_edge.messaging.exceptions import PublishError

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])
        with pytest.raises(PublishError, match="put failed"):
            await adapter.publish("test.subject", b"hello")


# ---------------------------------------------------------------------------
# TestZenohClientPublishQueryReply
# ---------------------------------------------------------------------------


class TestZenohClientPublishQueryReply:
    """Verify publish() intercepts query reply subjects."""

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_publish_to_query_prefix_replies_to_query(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])

        # Simulate a pending query (stored as (query, timestamp) tuple)
        import time
        mock_query = _make_mock_query("test/rpc", b"request")
        query_id = "abc123"
        adapter._pending_queries[query_id] = (mock_query, time.monotonic())

        # Publish to the reply subject
        await adapter.publish(f"_zenoh_query/{query_id}", b"response")

        # Verify query.reply was called instead of session.put
        mock_query.reply.assert_called_once()
        session.put.assert_not_called()
        assert query_id not in adapter._pending_queries

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_publish_to_query_missing_id_falls_through(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])

        # Publish to query prefix but with non-existent ID — falls through to put
        await adapter.publish("_zenoh_query/nonexistent", b"data")

        session.put.assert_called_once()


# ---------------------------------------------------------------------------
# TestZenohClientSubscribe
# ---------------------------------------------------------------------------


class TestZenohClientSubscribe:
    """Verify subscribe() declares subscriber + queryable."""

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_subscribe_declares_subscriber_and_queryable(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])

        async def handler(data, reply):
            pass

        sub = await adapter.subscribe("test.subject", handler)

        session.declare_subscriber.assert_called_once()
        session.declare_queryable.assert_called_once()

        # Key expression should be converted
        sub_key = session.declare_subscriber.call_args[0][0]
        assert sub_key == "test/subject"

        await sub.unsubscribe()

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_subscribe_converts_subject_syntax(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])

        async def handler(data, reply):
            pass

        await adapter.subscribe("device-connect.*.event.>", handler)

        sub_key = session.declare_subscriber.call_args[0][0]
        assert sub_key == "device-connect/*/event/**"

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_subscribe_callback_receives_sample_data(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])

        received = []

        async def handler(data, reply):
            received.append((data, reply))

        await adapter.subscribe("test.subject", handler)

        # Get the on_sample callback that was passed to declare_subscriber
        on_sample = session.declare_subscriber.call_args[0][1]

        # Simulate a Zenoh sample arriving
        sample = _make_mock_sample("test/subject", b'{"value": 42}')
        on_sample(sample)

        # Give the drain loop time to process
        await asyncio.sleep(0.1)

        assert len(received) == 1
        assert received[0][0] == b'{"value": 42}'
        assert received[0][1] is None  # No reply for pub/sub

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_subscribe_queryable_passes_reply_subject(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])

        received = []

        async def handler(data, reply):
            received.append((data, reply))

        await adapter.subscribe("test.rpc", handler)

        # Get the on_query callback that was passed to declare_queryable
        on_query = session.declare_queryable.call_args[0][1]

        # Simulate a Zenoh query arriving
        query = _make_mock_query("test/rpc", b'{"method": "ping"}')
        on_query(query)

        await asyncio.sleep(0.1)

        assert len(received) == 1
        assert received[0][0] == b'{"method": "ping"}'
        assert received[0][1] is not None  # Reply subject present
        assert received[0][1].startswith("_zenoh_query/")

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_subscribe_queue_group_logs_warning(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])

        async def handler(data, reply):
            pass

        with patch.object(adapter._logger, "warning") as mock_warn:
            await adapter.subscribe("test.subject", handler, queue="my-queue")
            mock_warn.assert_called_once()
            assert "queue" in mock_warn.call_args[0][0].lower()

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_subscribe_when_disconnected_raises(self, mock_zenoh):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter
        from device_connect_edge.messaging.exceptions import NotConnectedError

        adapter = ZenohAdapter()

        async def handler(data, reply):
            pass

        with pytest.raises(NotConnectedError):
            await adapter.subscribe("test.subject", handler)

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_unsubscribe_undeclares_both(self, mock_zenoh):
        session = _make_mock_session()
        mock_sub = MagicMock()
        mock_qable = MagicMock()
        session.declare_subscriber = MagicMock(return_value=mock_sub)
        session.declare_queryable = MagicMock(return_value=mock_qable)
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])

        async def handler(data, reply):
            pass

        sub = await adapter.subscribe("test.subject", handler)
        await sub.unsubscribe()

        mock_sub.undeclare.assert_called_once()
        mock_qable.undeclare.assert_called_once()

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_subscribe_only_skips_queryable(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])

        async def handler(data, reply):
            pass

        sub = await adapter.subscribe("test.subject", handler, subscribe_only=True)

        session.declare_subscriber.assert_called_once()
        session.declare_queryable.assert_not_called()

        await sub.unsubscribe()

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_unsubscribe_with_subscribe_only(self, mock_zenoh):
        session = _make_mock_session()
        mock_sub = MagicMock()
        session.declare_subscriber = MagicMock(return_value=mock_sub)
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])

        async def handler(data, reply):
            pass

        sub = await adapter.subscribe("test.subject", handler, subscribe_only=True)
        await sub.unsubscribe()

        mock_sub.undeclare.assert_called_once()
        # queryable was never created, so no undeclare expected
        session.declare_queryable.assert_not_called()


# ---------------------------------------------------------------------------
# TestZenohClientSubscribeWithSubject
# ---------------------------------------------------------------------------


class TestZenohClientSubscribeWithSubject:
    """Verify subscribe_with_subject() passes key expression."""

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_subscribe_with_subject_passes_key_expr(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])

        received = []

        async def handler(data, subject, reply):
            received.append((data, subject, reply))

        await adapter.subscribe_with_subject("test.*", handler)

        on_sample = session.declare_subscriber.call_args[0][1]
        sample = _make_mock_sample("test/hello", b"data")
        on_sample(sample)

        await asyncio.sleep(0.1)

        assert len(received) == 1
        assert received[0][1] == "test/hello"
        assert received[0][2] is None

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_subscribe_with_subject_queryable_reply(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])

        received = []

        async def handler(data, subject, reply):
            received.append((data, subject, reply))

        await adapter.subscribe_with_subject("test.rpc", handler)

        on_query = session.declare_queryable.call_args[0][1]
        query = _make_mock_query("test/rpc", b"request")
        on_query(query)

        await asyncio.sleep(0.1)

        assert len(received) == 1
        assert received[0][1] == "test/rpc"
        assert received[0][2] is not None

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_subscribe_with_subject_when_disconnected_raises(self, mock_zenoh):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter
        from device_connect_edge.messaging.exceptions import NotConnectedError

        adapter = ZenohAdapter()

        async def handler(data, subject, reply):
            pass

        with pytest.raises(NotConnectedError):
            await adapter.subscribe_with_subject("test.subject", handler)


# ---------------------------------------------------------------------------
# TestZenohClientRequest
# ---------------------------------------------------------------------------


class TestZenohClientRequest:
    """Verify request() uses session.get()."""

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_request_calls_session_get(self, mock_zenoh):
        session = _make_mock_session()
        reply = _make_mock_reply(b'{"result": "ok"}')
        session.get = MagicMock(return_value=[reply])
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])

        result = await adapter.request("test.rpc", b'{"method": "ping"}')

        session.get.assert_called_once()
        assert result == b'{"result": "ok"}'

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_request_returns_first_reply(self, mock_zenoh):
        session = _make_mock_session()
        reply1 = _make_mock_reply(b"first")
        reply2 = _make_mock_reply(b"second")
        session.get = MagicMock(return_value=[reply1, reply2])
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])

        result = await adapter.request("test.rpc", b"data")
        assert result == b"first"

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_request_custom_timeout(self, mock_zenoh):
        session = _make_mock_session()
        reply = _make_mock_reply(b"ok")
        session.get = MagicMock(return_value=[reply])
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])

        await adapter.request("test.rpc", b"data", timeout=10.0)

        call_kwargs = session.get.call_args
        # Full timeout is now passed through (CancellationToken handles
        # early termination instead of capping at 2s).
        assert call_kwargs[1]["timeout"] == 10.0
        assert call_kwargs[1]["cancellation_token"] is not None

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_request_cancels_token_on_ok_reply(self, mock_zenoh):
        """CancellationToken.cancel() is called after first ok reply."""
        session = _make_mock_session()
        reply = _make_mock_reply(b"ok")
        session.get = MagicMock(return_value=[reply])
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())
        mock_token = MagicMock()
        mock_zenoh.CancellationToken = MagicMock(return_value=mock_token)

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])

        result = await adapter.request("test.rpc", b"data")

        assert result == b"ok"
        mock_token.cancel.assert_called_once()

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_request_no_cancel_on_error_only(self, mock_zenoh):
        """CancellationToken is NOT cancelled when only error replies arrive."""
        session = _make_mock_session()
        reply = _make_mock_reply_err(b"some error")
        session.get = MagicMock(return_value=[reply])
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())
        mock_token = MagicMock()
        mock_zenoh.CancellationToken = MagicMock(return_value=mock_token)

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter
        from device_connect_edge.messaging.exceptions import PublishError

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])

        with pytest.raises(PublishError):
            await adapter.request("test.rpc", b"data")

        mock_token.cancel.assert_not_called()

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_request_timeout_raises(self, mock_zenoh):
        session = _make_mock_session()
        session.get = MagicMock(return_value=[])  # No replies
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter
        from device_connect_edge.messaging.exceptions import RequestTimeoutError

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])

        with pytest.raises(RequestTimeoutError, match="timed out"):
            await adapter.request("test.rpc", b"data", timeout=1.0)

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_request_when_disconnected_raises(self, mock_zenoh):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter
        from device_connect_edge.messaging.exceptions import NotConnectedError

        adapter = ZenohAdapter()
        with pytest.raises(NotConnectedError):
            await adapter.request("test.rpc", b"data")

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_request_error_reply_raises(self, mock_zenoh):
        session = _make_mock_session()
        reply = _make_mock_reply_err(b"query error")
        session.get = MagicMock(return_value=[reply])
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter
        from device_connect_edge.messaging.exceptions import PublishError

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])

        with pytest.raises(PublishError, match="query error"):
            await adapter.request("test.rpc", b"data")

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_request_d2d_retries_on_no_responders(self, mock_zenoh):
        """D2D mode retries when session.get returns zero replies."""
        session = _make_mock_session()
        reply = _make_mock_reply(b"ok")
        # First call returns no replies, second returns a reply
        session.get = MagicMock(side_effect=[[], [reply]])
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())
        mock_zenoh.CancellationToken = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["zenoh://"])  # peer mode → d2d
        adapter._d2d_retry_delay = 0.01  # speed up test

        result = await adapter.request("test.rpc", b"data")
        assert result == b"ok"
        assert session.get.call_count == 2

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_request_d2d_uses_query_target_all(self, mock_zenoh):
        """D2D mode passes QueryTarget.ALL to session.get()."""
        session = _make_mock_session()
        reply = _make_mock_reply(b"ok")
        session.get = MagicMock(return_value=[reply])
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())
        mock_zenoh.CancellationToken = MagicMock(return_value=MagicMock())
        mock_zenoh.QueryTarget.ALL = "ALL"
        mock_zenoh.ConsolidationMode.NONE = "NONE"

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["zenoh://"])

        await adapter.request("test.rpc", b"data")

        call_kwargs = session.get.call_args[1]
        assert call_kwargs["target"] == "ALL"
        assert call_kwargs["consolidation"] == "NONE"

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_request_router_mode_no_retry(self, mock_zenoh):
        """Router mode does NOT retry on no responders."""
        session = _make_mock_session()
        session.get = MagicMock(return_value=[])
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())
        mock_zenoh.CancellationToken = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter
        from device_connect_edge.messaging.exceptions import RequestTimeoutError

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])  # router mode

        with pytest.raises(RequestTimeoutError):
            await adapter.request("test.rpc", b"data", timeout=0.1)
        # Only 1 attempt in router mode
        assert session.get.call_count == 1

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_configure_d2d_retry(self, mock_zenoh):
        """configure_d2d_retry updates retry parameters."""
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        adapter.configure_d2d_retry(retries=5, delay=1.0)

        assert adapter._d2d_retry_count == 5
        assert adapter._d2d_retry_delay == 1.0


# ---------------------------------------------------------------------------
# TestZenohClientClose
# ---------------------------------------------------------------------------


class TestZenohClientClose:
    """Verify close() cleans up resources."""

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_close_closes_session(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])
        await adapter.close()

        session.close.assert_called_once()

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_close_sets_state(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])
        await adapter.close()

        assert adapter._connected is False
        assert adapter._closed is True

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_close_cancels_drain_tasks(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])

        async def handler(data, reply):
            pass

        await adapter.subscribe("test.subject", handler)
        assert len(adapter._subscriptions) == 1

        await adapter.close()
        assert len(adapter._subscriptions) == 0

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_close_clears_pending_queries(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])
        adapter._pending_queries["q1"] = MagicMock()

        await adapter.close()
        assert len(adapter._pending_queries) == 0

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_close_undeclares_queryables_before_subscribers(self, mock_zenoh):
        """Phased teardown: queryable.undeclare() must happen before subscriber.undeclare()."""
        session = _make_mock_session()
        mock_sub = MagicMock()
        mock_qable = MagicMock()
        session.declare_subscriber = MagicMock(return_value=mock_sub)
        session.declare_queryable = MagicMock(return_value=mock_qable)
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])

        async def handler(data, reply):
            pass

        await adapter.subscribe("test.subject", handler)

        call_order = []
        mock_qable.undeclare = MagicMock(side_effect=lambda: call_order.append("queryable"))
        mock_sub.undeclare = MagicMock(side_effect=lambda: call_order.append("subscriber"))

        await adapter.close()

        assert call_order == ["queryable", "subscriber"]


# ---------------------------------------------------------------------------
# TestZenohClientProperties
# ---------------------------------------------------------------------------


class TestZenohClientProperties:
    """Verify is_connected / is_closed state tracking."""

    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    def test_is_connected_initially_false(self, mock_zenoh):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        assert adapter.is_connected is False

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_is_connected_true_after_connect(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])
        assert adapter.is_connected is True

    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    def test_is_closed_initially_false(self, mock_zenoh):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        assert adapter.is_closed is False

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_is_closed_true_after_close(self, mock_zenoh):
        session = _make_mock_session()
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])
        await adapter.close()
        assert adapter.is_closed is True

    @pytest.mark.asyncio
    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    async def test_is_connected_false_after_close(self, mock_zenoh):
        session = _make_mock_session()
        session.is_closed = MagicMock(return_value=True)
        mock_zenoh.open = MagicMock(return_value=session)
        mock_zenoh.Config.from_json5 = MagicMock(return_value=MagicMock())

        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        await adapter.connect(servers=["tcp/host:7447"])
        await adapter.close()
        assert adapter.is_connected is False

    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    def test_is_connected_false_when_session_none(self, mock_zenoh):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        adapter._session = None
        assert adapter.is_connected is False


# ---------------------------------------------------------------------------
# TestSubjectConversion
# ---------------------------------------------------------------------------


class TestSubjectConversion:
    """Verify convert_subject_syntax() NATS→Zenoh mapping."""

    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    def test_dots_to_slashes(self, mock_zenoh):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        assert adapter.convert_subject_syntax("device-connect.default.device.event") == "device-connect/default/device/event"

    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    def test_single_wildcard_unchanged(self, mock_zenoh):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        assert adapter.convert_subject_syntax("device-connect.*.event") == "device-connect/*/event"

    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    def test_multi_level_gt_to_double_star(self, mock_zenoh):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        assert adapter.convert_subject_syntax("device-connect.tenant.>") == "device-connect/tenant/**"

    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    def test_mixed_wildcards(self, mock_zenoh):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        assert adapter.convert_subject_syntax("device-connect.*.event.>") == "device-connect/*/event/**"

    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    def test_no_wildcards(self, mock_zenoh):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        assert adapter.convert_subject_syntax("simple.topic") == "simple/topic"

    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    def test_already_slash_format_passthrough(self, mock_zenoh):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        assert adapter.convert_subject_syntax("already/slash/format") == "already/slash/format"

    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    def test_single_segment(self, mock_zenoh):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        assert adapter.convert_subject_syntax("topic") == "topic"


# ---------------------------------------------------------------------------
# TestURLParsing
# ---------------------------------------------------------------------------


class TestURLParsing:
    """Verify _parse_server_url() handles various formats."""

    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    def test_zenoh_empty_is_peer_mode(self, mock_zenoh):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        assert adapter._parse_server_url("zenoh://") is None

    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    def test_zenoh_with_host(self, mock_zenoh):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        assert adapter._parse_server_url("zenoh://host:7447") == "tcp/host:7447"

    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    def test_zenoh_tls_with_host(self, mock_zenoh):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        assert adapter._parse_server_url("zenoh+tls://host:7447") == "tls/host:7447"

    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    def test_native_tcp_passthrough(self, mock_zenoh):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        assert adapter._parse_server_url("tcp/host:7447") == "tcp/host:7447"

    @patch("device_connect_edge.messaging.zenoh_adapter.zenoh")
    @patch("device_connect_edge.messaging.zenoh_adapter._ZENOH_AVAILABLE", True)
    def test_default_port(self, mock_zenoh):
        from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

        adapter = ZenohAdapter()
        assert adapter._parse_server_url("zenoh://host") == "tcp/host:7447"


# ---------------------------------------------------------------------------
# TestFactoryRegistration
# ---------------------------------------------------------------------------


class TestFactoryRegistration:
    """Verify Zenoh is a built-in backend in the factory."""

    def test_create_client_zenoh(self):
        from device_connect_edge.messaging import create_client

        client = create_client("zenoh")
        assert type(client).__name__ == "ZenohAdapter"

    def test_create_client_zenoh_case_insensitive(self):
        from device_connect_edge.messaging import create_client

        client = create_client("Zenoh")
        assert type(client).__name__ == "ZenohAdapter"

    def test_create_client_nats_still_works(self):
        from device_connect_edge.messaging import create_client

        client = create_client("nats")
        assert type(client).__name__ == "NATSAdapter"

    def test_create_client_unsupported_raises(self):
        from device_connect_edge.messaging import create_client

        with pytest.raises(ValueError, match="Unsupported messaging backend"):
            create_client("nonexistent")
