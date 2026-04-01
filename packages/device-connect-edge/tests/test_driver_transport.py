"""Tests for DriverTransport — raw messaging transport for DeviceDriver."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from device_connect_edge.drivers.base import DeviceDriver
from device_connect_edge.drivers.transport import DriverTransport


class MinimalDriver(DeviceDriver):
    device_type = "test"

    async def connect(self):
        pass

    async def disconnect(self):
        pass


class TestDriverTransportProperty:
    """Test the transport property on DeviceDriver."""

    def test_transport_none_before_set_device(self):
        driver = MinimalDriver()
        assert driver.transport is None

    def test_transport_none_before_messaging_connected(self):
        driver = MinimalDriver()
        device = MagicMock()
        device.messaging = None
        driver.set_device(device)
        assert driver.transport is None

    def test_transport_available_after_device_and_messaging(self):
        driver = MinimalDriver()
        device = MagicMock()
        device.messaging = AsyncMock()
        driver.set_device(device)
        t = driver.transport
        assert isinstance(t, DriverTransport)

    def test_transport_is_cached(self):
        driver = MinimalDriver()
        device = MagicMock()
        device.messaging = AsyncMock()
        driver.set_device(device)
        t1 = driver.transport
        t2 = driver.transport
        assert t1 is t2


class TestDriverTransport:
    """Test DriverTransport methods."""

    @pytest.fixture
    def transport(self):
        messaging = AsyncMock()
        return DriverTransport(messaging)

    @pytest.mark.asyncio
    async def test_publish_delegates(self, transport):
        await transport.publish("reachy_mini/command", b'{"head_pose": []}')
        transport._messaging.publish.assert_awaited_once_with(
            "reachy_mini/command", b'{"head_pose": []}'
        )

    @pytest.mark.asyncio
    async def test_subscribe_delegates(self, transport):
        cb = AsyncMock()
        sub_mock = AsyncMock()
        transport._messaging.subscribe.return_value = sub_mock

        result = await transport.subscribe("reachy_mini/joint_positions", cb)

        transport._messaging.subscribe.assert_awaited_once_with(
            "reachy_mini/joint_positions", cb
        )
        assert result is sub_mock
        assert sub_mock in transport._subscriptions

    @pytest.mark.asyncio
    async def test_request_delegates(self, transport):
        transport._messaging.request.return_value = b'{"ok": true}'
        result = await transport.request("some/topic", b'{"q": 1}', timeout=3.0)
        transport._messaging.request.assert_awaited_once_with(
            "some/topic", b'{"q": 1}', timeout=3.0
        )
        assert result == b'{"ok": true}'

    @pytest.mark.asyncio
    async def test_teardown_unsubscribes_all(self, transport):
        sub1 = AsyncMock()
        sub2 = AsyncMock()
        sub3 = AsyncMock()
        transport._subscriptions = [sub1, sub2, sub3]

        await transport.teardown()

        sub1.unsubscribe.assert_awaited_once()
        sub2.unsubscribe.assert_awaited_once()
        sub3.unsubscribe.assert_awaited_once()
        assert transport._subscriptions == []

    @pytest.mark.asyncio
    async def test_teardown_tolerates_errors(self, transport):
        sub_ok = AsyncMock()
        sub_err = AsyncMock()
        sub_err.unsubscribe.side_effect = RuntimeError("boom")
        transport._subscriptions = [sub_err, sub_ok]

        await transport.teardown()  # Should not raise

        sub_ok.unsubscribe.assert_awaited_once()
        assert transport._subscriptions == []


class TestTeardownIntegration:
    """Test that teardown_subscriptions calls transport.teardown()."""

    @pytest.mark.asyncio
    async def test_teardown_subscriptions_tears_down_transport(self):
        driver = MinimalDriver()
        device = MagicMock()
        device.messaging = AsyncMock()
        driver.set_device(device)

        # Access transport to create it
        t = driver.transport
        sub = AsyncMock()
        t._subscriptions = [sub]

        await driver.teardown_subscriptions()

        sub.unsubscribe.assert_awaited_once()
        assert t._subscriptions == []
