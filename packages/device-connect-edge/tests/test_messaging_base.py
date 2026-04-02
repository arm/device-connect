"""Unit tests for device_connect_edge.messaging.base -- MessagingClient ABC contract.

Validates that the abstract interface is correctly defined and that
the default implementations behave as expected.
"""

import pytest

from device_connect_edge.messaging.base import MessagingClient, Subscription


class TestSubscriptionABC:
    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            Subscription()

    def test_subclass_must_implement_unsubscribe(self):
        class BadSub(Subscription):
            pass

        with pytest.raises(TypeError):
            BadSub()

    def test_valid_subclass(self):
        class GoodSub(Subscription):
            async def unsubscribe(self) -> None:
                pass

        sub = GoodSub()
        assert sub is not None


class TestMessagingClientABC:
    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            MessagingClient()

    def test_subclass_must_implement_all_abstract(self):
        class PartialClient(MessagingClient):
            async def connect(self, servers, **kwargs):
                pass
            # Missing publish, subscribe, request, close, is_connected, is_closed

        with pytest.raises(TypeError):
            PartialClient()


class TestMessagingClientDefaults:
    """Test default (non-abstract) method implementations."""

    def _make_client(self):
        """Create a minimal concrete MessagingClient for testing defaults."""
        class MinimalClient(MessagingClient):
            async def connect(self, servers, credentials=None, tls_config=None,
                              reconnect_cb=None, disconnect_cb=None, **kwargs):
                pass
            async def publish(self, subject, data):
                pass
            async def subscribe(self, subject, callback, queue=None):
                pass
            async def request(self, subject, data, timeout=5.0):
                pass
            async def close(self):
                pass
            @property
            def is_connected(self):
                return True
            @property
            def is_closed(self):
                return False

        return MinimalClient()

    def test_convert_subject_syntax_passthrough(self):
        client = self._make_client()
        assert client.convert_subject_syntax("device-connect.tenant.device.event") == "device-connect.tenant.device.event"

    @pytest.mark.asyncio
    async def test_drain_default_noop(self):
        client = self._make_client()
        await client.drain()  # Should not raise

    @pytest.mark.asyncio
    async def test_subscribe_with_subject_raises_not_implemented(self):
        client = self._make_client()
        with pytest.raises(NotImplementedError):
            await client.subscribe_with_subject("test", lambda d, s, r: None)
