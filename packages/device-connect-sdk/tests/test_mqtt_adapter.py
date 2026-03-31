"""Unit tests for device_connect_sdk.messaging.mqtt_adapter — MQTTAdapter.

The ``aiomqtt`` library is fully mocked so no real MQTT broker is needed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_mqtt_client():
    """Return a mock aiomqtt.Client with sane defaults."""
    mc = MagicMock()
    mc.publish = AsyncMock()
    mc.subscribe = AsyncMock()
    mc.unsubscribe = AsyncMock()
    mc.__aenter__ = AsyncMock(return_value=mc)
    mc.__aexit__ = AsyncMock(return_value=False)
    # messages is an async iterator; tests that need it can override
    mc.messages = AsyncMock()
    mc.messages.__aiter__ = MagicMock(return_value=iter([]))
    return mc


# ---------------------------------------------------------------------------
# TestMQTTClientInit
# ---------------------------------------------------------------------------


class TestMQTTClientInit:
    """Constructor and default attribute values."""

    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient", new_callable=lambda: MagicMock)
    def test_constructor_defaults(self, _MockClient):
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        assert adapter._client is None
        assert adapter._connected is False
        assert adapter._closed is False
        assert adapter._qos == 1
        assert adapter._subscriptions == {}

    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient", None)
    def test_constructor_raises_when_aiomqtt_missing(self):
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        with pytest.raises(ImportError, match="aiomqtt"):
            MQTTAdapter()


# ---------------------------------------------------------------------------
# TestMQTTClientConnect
# ---------------------------------------------------------------------------


class TestMQTTClientConnect:
    """Verify connect() parses URLs and passes credentials/TLS."""

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_connect_parses_hostname_and_port(self, MockClient):
        mock_client = _make_mock_mqtt_client()
        MockClient.return_value = mock_client
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        await adapter.connect(servers=["mqtt://broker.local:1883"])

        call_kwargs = MockClient.call_args[1]
        assert call_kwargs["hostname"] == "broker.local"
        assert call_kwargs["port"] == 1883
        assert adapter._connected is True

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_connect_default_port(self, MockClient):
        mock_client = _make_mock_mqtt_client()
        MockClient.return_value = mock_client
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        await adapter.connect(servers=["mqtt://broker.local"])

        call_kwargs = MockClient.call_args[1]
        assert call_kwargs["port"] == 1883

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_connect_with_credentials(self, MockClient):
        mock_client = _make_mock_mqtt_client()
        MockClient.return_value = mock_client
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        await adapter.connect(
            servers=["mqtt://broker.local:1883"],
            credentials={"username": "user1", "password": "pass1"},
        )

        call_kwargs = MockClient.call_args[1]
        assert call_kwargs["username"] == "user1"
        assert call_kwargs["password"] == "pass1"

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_connect_with_tls_config(self, MockClient):
        mock_client = _make_mock_mqtt_client()
        MockClient.return_value = mock_client
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        with patch.object(adapter, "_build_tls_context", return_value="fake_ctx") as mock_tls:
            await adapter.connect(
                servers=["mqtts://broker.local:8883"],
                tls_config={"ca_file": "/tmp/ca.pem"},
            )
            mock_tls.assert_called_once_with({"ca_file": "/tmp/ca.pem"})
            call_kwargs = MockClient.call_args[1]
            assert call_kwargs["tls_context"] == "fake_ctx"

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_connect_tls_detected_from_mqtts_scheme(self, MockClient):
        mock_client = _make_mock_mqtt_client()
        MockClient.return_value = mock_client
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        with patch.object(adapter, "_build_tls_context", return_value="ctx"):
            await adapter.connect(servers=["mqtts://broker.local:8883"])
            call_kwargs = MockClient.call_args[1]
            assert "tls_context" in call_kwargs

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_connect_custom_qos_and_keepalive(self, MockClient):
        mock_client = _make_mock_mqtt_client()
        MockClient.return_value = mock_client
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        await adapter.connect(
            servers=["mqtt://broker.local:1883"],
            qos=2,
            keepalive=30,
        )

        assert adapter._qos == 2
        call_kwargs = MockClient.call_args[1]
        assert call_kwargs["keepalive"] == 30

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_connect_calls_aenter(self, MockClient):
        mock_client = _make_mock_mqtt_client()
        MockClient.return_value = mock_client
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        await adapter.connect(servers=["mqtt://broker.local:1883"])

        mock_client.__aenter__.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_connect_failure_raises_connection_error(self, MockClient):
        mock_client = _make_mock_mqtt_client()
        mock_client.__aenter__ = AsyncMock(side_effect=Exception("refused"))
        MockClient.return_value = mock_client
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter
        from device_connect_sdk.messaging.exceptions import ConnectionError

        adapter = MQTTAdapter()
        with pytest.raises(ConnectionError, match="refused"):
            await adapter.connect(servers=["mqtt://broker.local:1883"])

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_connect_auth_failure_raises_authentication_error(self, MockClient):
        mock_client = _make_mock_mqtt_client()
        mock_client.__aenter__ = AsyncMock(
            side_effect=Exception("authentication failed")
        )
        MockClient.return_value = mock_client
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter
        from device_connect_sdk.messaging.exceptions import AuthenticationError

        adapter = MQTTAdapter()
        with pytest.raises(AuthenticationError, match="Authentication failed"):
            await adapter.connect(servers=["mqtt://broker.local:1883"])

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_connect_multi_server_uses_first(self, MockClient):
        """MQTT only supports one broker; second server is ignored."""
        mock_client = _make_mock_mqtt_client()
        MockClient.return_value = mock_client
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        await adapter.connect(
            servers=["mqtt://primary:1883", "mqtt://secondary:1883"]
        )

        call_kwargs = MockClient.call_args[1]
        assert call_kwargs["hostname"] == "primary"


# ---------------------------------------------------------------------------
# TestMQTTClientPublish
# ---------------------------------------------------------------------------


class TestMQTTClientPublish:
    """Verify publish() converts subjects and delegates to client."""

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_publish_converts_dots_to_slashes(self, MockClient):
        mock_client = _make_mock_mqtt_client()
        MockClient.return_value = mock_client
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        await adapter.connect(servers=["mqtt://broker:1883"])
        await adapter.publish("device-connect.tenant.device.event", b"payload")

        mock_client.publish.assert_awaited_once_with(
            "device-connect/tenant/device/event", b"payload", qos=1
        )

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_publish_when_disconnected_raises(self, MockClient):
        MockClient.return_value = _make_mock_mqtt_client()
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter
        from device_connect_sdk.messaging.exceptions import NotConnectedError

        adapter = MQTTAdapter()
        # Not connected (_connected is False by default)
        with pytest.raises(NotConnectedError):
            await adapter.publish("subject", b"data")

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_publish_error_raises_publish_error(self, MockClient):
        mock_client = _make_mock_mqtt_client()
        mock_client.publish = AsyncMock(side_effect=Exception("broker down"))
        MockClient.return_value = mock_client
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter
        from device_connect_sdk.messaging.exceptions import PublishError

        adapter = MQTTAdapter()
        await adapter.connect(servers=["mqtt://broker:1883"])
        with pytest.raises(PublishError, match="broker down"):
            await adapter.publish("subject", b"data")


# ---------------------------------------------------------------------------
# TestMQTTClientSubscribe
# ---------------------------------------------------------------------------


class TestMQTTClientSubscribe:
    """Verify subscribe() with topic conversion and queue groups."""

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_subscribe_converts_subject(self, MockClient):
        mock_client = _make_mock_mqtt_client()
        MockClient.return_value = mock_client
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        await adapter.connect(servers=["mqtt://broker:1883"])
        await adapter.subscribe("device-connect.tenant.device.event", AsyncMock())

        mock_client.subscribe.assert_awaited_once_with(
            "device-connect/tenant/device/event", qos=1
        )

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_subscribe_with_queue_uses_shared_prefix(self, MockClient):
        mock_client = _make_mock_mqtt_client()
        MockClient.return_value = mock_client
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        await adapter.connect(servers=["mqtt://broker:1883"])
        await adapter.subscribe("events.>", AsyncMock(), queue="workers")

        mock_client.subscribe.assert_awaited_once_with(
            "$share/workers/events/#", qos=1
        )

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_subscribe_stores_callback(self, MockClient):
        mock_client = _make_mock_mqtt_client()
        MockClient.return_value = mock_client
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        cb = AsyncMock()
        adapter = MQTTAdapter()
        await adapter.connect(servers=["mqtt://broker:1883"])
        await adapter.subscribe("events.test", cb)

        assert "events/test" in adapter._subscriptions
        assert adapter._subscriptions["events/test"] is cb

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_subscribe_returns_subscription_wrapper(self, MockClient):
        mock_client = _make_mock_mqtt_client()
        MockClient.return_value = mock_client
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter, MQTTSubscriptionWrapper

        adapter = MQTTAdapter()
        await adapter.connect(servers=["mqtt://broker:1883"])
        sub = await adapter.subscribe("events.test", AsyncMock())

        assert isinstance(sub, MQTTSubscriptionWrapper)

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_subscribe_when_disconnected_raises(self, MockClient):
        MockClient.return_value = _make_mock_mqtt_client()
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter
        from device_connect_sdk.messaging.exceptions import NotConnectedError

        adapter = MQTTAdapter()
        with pytest.raises(NotConnectedError):
            await adapter.subscribe("subject", AsyncMock())

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_subscribe_error_raises_subscribe_error(self, MockClient):
        mock_client = _make_mock_mqtt_client()
        mock_client.subscribe = AsyncMock(side_effect=Exception("bad topic"))
        MockClient.return_value = mock_client
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter
        from device_connect_sdk.messaging.exceptions import SubscribeError

        adapter = MQTTAdapter()
        await adapter.connect(servers=["mqtt://broker:1883"])
        with pytest.raises(SubscribeError, match="bad topic"):
            await adapter.subscribe("subject", AsyncMock())

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_unsubscribe_delegates_to_client(self, MockClient):
        mock_client = _make_mock_mqtt_client()
        MockClient.return_value = mock_client
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        await adapter.connect(servers=["mqtt://broker:1883"])
        sub = await adapter.subscribe("events.test", AsyncMock())
        await sub.unsubscribe()

        mock_client.unsubscribe.assert_awaited_once_with("events/test")


# ---------------------------------------------------------------------------
# TestMQTTClientRequest
# ---------------------------------------------------------------------------


class TestMQTTClientRequest:
    """Verify manual request/reply pattern."""

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_request_subscribes_to_reply_topic(self, MockClient):
        mock_client = _make_mock_mqtt_client()
        MockClient.return_value = mock_client
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter
        from device_connect_sdk.messaging.exceptions import RequestTimeoutError

        adapter = MQTTAdapter()
        await adapter.connect(servers=["mqtt://broker:1883"])

        # The request will time out because no reply arrives, but we
        # verify that it subscribed to a _reply/ topic
        with pytest.raises(RequestTimeoutError):
            await adapter.request("service.method", b"data", timeout=0.01)

        # At least one subscribe call should be to a _reply/ topic
        subscribe_calls = mock_client.subscribe.call_args_list
        reply_subs = [c for c in subscribe_calls if "_reply/" in str(c)]
        assert len(reply_subs) >= 1

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_request_when_disconnected_raises(self, MockClient):
        MockClient.return_value = _make_mock_mqtt_client()
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter
        from device_connect_sdk.messaging.exceptions import NotConnectedError

        adapter = MQTTAdapter()
        with pytest.raises(NotConnectedError):
            await adapter.request("subject", b"data")

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_request_cleans_up_future_on_timeout(self, MockClient):
        mock_client = _make_mock_mqtt_client()
        MockClient.return_value = mock_client
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter
        from device_connect_sdk.messaging.exceptions import RequestTimeoutError

        adapter = MQTTAdapter()
        await adapter.connect(servers=["mqtt://broker:1883"])

        with pytest.raises(RequestTimeoutError):
            await adapter.request("service.method", b"data", timeout=0.01)

        # After timeout, the future should be cleaned up
        assert len(adapter._request_futures) == 0


# ---------------------------------------------------------------------------
# TestMQTTClientClose
# ---------------------------------------------------------------------------


class TestMQTTClientClose:
    """Verify close() cleans up resources."""

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_close_calls_aexit(self, MockClient):
        mock_client = _make_mock_mqtt_client()
        MockClient.return_value = mock_client
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        await adapter.connect(servers=["mqtt://broker:1883"])
        await adapter.close()

        mock_client.__aexit__.assert_awaited_once()
        assert adapter._connected is False
        assert adapter._closed is True

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_close_cancels_message_loop(self, MockClient):
        mock_client = _make_mock_mqtt_client()
        MockClient.return_value = mock_client
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        await adapter.connect(servers=["mqtt://broker:1883"])

        # The message loop task should exist
        assert adapter._message_loop_task is not None

        await adapter.close()
        # After close, connected should be False
        assert adapter._connected is False


# ---------------------------------------------------------------------------
# TestMQTTClientProperties
# ---------------------------------------------------------------------------


class TestMQTTClientProperties:
    """Verify is_connected and is_closed properties."""

    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient", new_callable=lambda: MagicMock)
    def test_is_connected_initially_false(self, _MockClient):
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        assert adapter.is_connected is False

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_is_connected_true_after_connect(self, MockClient):
        MockClient.return_value = _make_mock_mqtt_client()
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        await adapter.connect(servers=["mqtt://broker:1883"])
        assert adapter.is_connected is True

    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient", new_callable=lambda: MagicMock)
    def test_is_closed_initially_false(self, _MockClient):
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        assert adapter.is_closed is False

    @pytest.mark.asyncio
    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient")
    async def test_is_closed_true_after_close(self, MockClient):
        MockClient.return_value = _make_mock_mqtt_client()
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        await adapter.connect(servers=["mqtt://broker:1883"])
        await adapter.close()
        assert adapter.is_closed is True


# ---------------------------------------------------------------------------
# TestSubjectConversion
# ---------------------------------------------------------------------------


class TestSubjectConversion:
    """Verify convert_subject_syntax (dots to slashes, wildcard mapping)."""

    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient", new_callable=lambda: MagicMock)
    def test_dots_to_slashes(self, _MockClient):
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        assert adapter.convert_subject_syntax("a.b.c") == "a/b/c"

    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient", new_callable=lambda: MagicMock)
    def test_single_wildcard_star_to_plus(self, _MockClient):
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        assert adapter.convert_subject_syntax("a.*.c") == "a/+/c"

    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient", new_callable=lambda: MagicMock)
    def test_trailing_star_to_plus(self, _MockClient):
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        assert adapter.convert_subject_syntax("a.b.*") == "a/b/+"

    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient", new_callable=lambda: MagicMock)
    def test_multi_level_wildcard_gt_to_hash(self, _MockClient):
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        assert adapter.convert_subject_syntax("device-connect.default.>") == "device-connect/default/#"

    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient", new_callable=lambda: MagicMock)
    def test_mixed_wildcards(self, _MockClient):
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        result = adapter.convert_subject_syntax("device-connect.*.event.*")
        assert result == "device-connect/+/event/+"

    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient", new_callable=lambda: MagicMock)
    def test_no_wildcards(self, _MockClient):
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        assert adapter.convert_subject_syntax("exact.topic") == "exact/topic"


# ---------------------------------------------------------------------------
# TestTopicMatching
# ---------------------------------------------------------------------------


class TestTopicMatching:
    """Verify _topic_matches wildcard logic."""

    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient", new_callable=lambda: MagicMock)
    def test_exact_match(self, _MockClient):
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        assert adapter._topic_matches("a/b/c", "a/b/c") is True

    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient", new_callable=lambda: MagicMock)
    def test_exact_no_match(self, _MockClient):
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        assert adapter._topic_matches("a/b/c", "a/b/d") is False

    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient", new_callable=lambda: MagicMock)
    def test_single_level_wildcard(self, _MockClient):
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        assert adapter._topic_matches("a/x/c", "a/+/c") is True

    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient", new_callable=lambda: MagicMock)
    def test_multi_level_wildcard(self, _MockClient):
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        assert adapter._topic_matches("a/b/c/d", "a/#") is True

    @patch("device_connect_sdk.messaging.mqtt_adapter.MQTTClient", new_callable=lambda: MagicMock)
    def test_shared_subscription_prefix_stripped(self, _MockClient):
        from device_connect_sdk.messaging.mqtt_adapter import MQTTAdapter

        adapter = MQTTAdapter()
        assert adapter._topic_matches("a/b/c", "$share/group/a/b/c") is True
