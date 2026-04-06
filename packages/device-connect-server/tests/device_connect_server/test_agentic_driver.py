"""Tests for D2D (device-to-device) communication in DeviceDriver."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from device_connect_edge.drivers.base import DeviceDriver, on
from device_connect_edge.drivers.decorators import rpc
from device_connect_edge.types import DeviceIdentity, DeviceStatus


class TestDeviceDriver:
    """Tests for DeviceDriver base class."""

    def create_driver(self):
        """Create a test driver instance."""

        class TestDeviceDriver(DeviceDriver):
            device_type = "test_agentic"

            @property
            def identity(self) -> DeviceIdentity:
                return DeviceIdentity(
                    device_type="test_agentic",
                    manufacturer="Test",
                    description="Test agentic driver"
                )

            @property
            def status(self) -> DeviceStatus:
                return DeviceStatus(location="test-zone")

            @rpc()
            async def test_function(self, value: str) -> dict:
                """A test function."""
                return {"value": value}

            async def connect(self) -> None:
                pass

            async def disconnect(self) -> None:
                pass

        return TestDeviceDriver()

    def test_initialization(self):
        """Test driver initialization."""
        driver = self.create_driver()

        assert driver.router is None
        assert driver.registry is None

    def test_set_router(self):
        """Test setting router property."""
        driver = self.create_driver()
        mock_router = MagicMock()

        driver.router = mock_router
        assert driver.router == mock_router

    def test_set_registry(self):
        """Test setting registry property."""
        driver = self.create_driver()
        mock_registry = MagicMock()

        driver.registry = mock_registry
        assert driver.registry == mock_registry

    @pytest.mark.asyncio
    async def test_invoke_remote_without_router(self):
        """Test invoke_remote raises error without router."""
        driver = self.create_driver()

        with pytest.raises(RuntimeError, match="Router not configured"):
            await driver.invoke_remote("device-001", "some_function")

    @pytest.mark.asyncio
    async def test_invoke_remote_with_router(self):
        """Test invoke_remote calls router correctly."""
        driver = self.create_driver()
        mock_router = AsyncMock()
        mock_router.invoke.return_value = {"result": "success"}

        driver.router = mock_router

        result = await driver.invoke_remote(
            "robot-001",
            "start_cleaning",
            zone="A"
        )

        # Check basic call structure (trace_id and _dc_meta are added automatically)
        mock_router.invoke.assert_called_once()
        call_args = mock_router.invoke.call_args
        assert call_args[0] == ("robot-001", "start_cleaning")
        assert "zone" in call_args[1]["params"]
        assert call_args[1]["params"]["zone"] == "A"
        # Verify trace metadata was injected
        assert "_dc_meta" in call_args[1]["params"]
        assert "trace_id" in call_args[1]["params"]["_dc_meta"]
        assert "source_device" in call_args[1]["params"]["_dc_meta"]
        assert result == {"result": "success"}

    @pytest.mark.asyncio
    async def test_invoke_remote_with_timeout(self):
        """Test invoke_remote passes timeout."""
        driver = self.create_driver()
        mock_router = AsyncMock()
        mock_router.invoke.return_value = {"result": "success"}

        driver.router = mock_router

        await driver.invoke_remote(
            "robot-001",
            "start_cleaning",
            timeout=5.0,
            zone="A"
        )

        # Check basic call structure (trace_id and _dc_meta are added automatically)
        mock_router.invoke.assert_called_once()
        call_args = mock_router.invoke.call_args
        assert call_args[0] == ("robot-001", "start_cleaning")
        assert call_args[1]["timeout"] == 5.0
        assert "zone" in call_args[1]["params"]
        assert call_args[1]["params"]["zone"] == "A"
        # Verify trace metadata was injected
        assert "_dc_meta" in call_args[1]["params"]
        assert "trace_id" in call_args[1]["params"]["_dc_meta"]

    @pytest.mark.asyncio
    async def test_list_devices_without_registry(self):
        """Test list_devices raises error without registry."""
        driver = self.create_driver()

        with pytest.raises(RuntimeError, match="Registry not configured"):
            await driver.list_devices()

    @pytest.mark.asyncio
    async def test_list_devices_with_registry(self):
        """Test list_devices calls registry correctly."""
        driver = self.create_driver()
        mock_registry = AsyncMock()
        mock_registry.list_devices.return_value = [
            {"device_id": "camera-001", "type": "camera"},
            {"device_id": "camera-002", "type": "camera"},
        ]

        driver.registry = mock_registry

        result = await driver.list_devices(device_type="camera")

        mock_registry.list_devices.assert_called_once_with(
            device_type="camera",
            location=None,
            capabilities=None,
        )
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_get_device_without_registry(self):
        """Test get_device raises error without registry."""
        driver = self.create_driver()

        with pytest.raises(RuntimeError, match="Registry not configured"):
            await driver.get_device("camera-001")

    @pytest.mark.asyncio
    async def test_get_device_with_registry(self):
        """Test get_device calls registry correctly."""
        driver = self.create_driver()
        mock_registry = AsyncMock()
        mock_registry.get_device.return_value = {
            "device_id": "camera-001",
            "type": "camera"
        }

        driver.registry = mock_registry

        result = await driver.get_device("camera-001")

        mock_registry.get_device.assert_called_once_with("camera-001")
        assert result["device_id"] == "camera-001"


class TestEventSubscription:
    """Tests for @on decorator."""

    def test_decorator_marks_method(self):
        """Test that decorator marks method correctly."""

        class TestDriver(DeviceDriver):
            device_type = "test"

            @property
            def identity(self) -> DeviceIdentity:
                return DeviceIdentity(device_type="test")

            @property
            def status(self) -> DeviceStatus:
                return DeviceStatus()

            @on(device_type="camera", event_name="motion_detected")
            async def on_motion(self, device_id: str, event_name: str, payload: dict):
                pass

            async def connect(self) -> None:
                pass

            async def disconnect(self) -> None:
                pass

        driver = TestDriver()
        method = driver.on_motion

        assert method._is_event_subscription is True
        assert method._sub_device_type == "camera"
        assert method._sub_event_name == "motion_detected"
        assert method._sub_device_id is None

    def test_decorator_with_device_id(self):
        """Test decorator with specific device_id."""

        class TestDriver(DeviceDriver):
            device_type = "test"

            @property
            def identity(self) -> DeviceIdentity:
                return DeviceIdentity(device_type="test")

            @property
            def status(self) -> DeviceStatus:
                return DeviceStatus()

            @on(device_id="camera-001")
            async def on_camera_event(self, device_id: str, event_name: str, payload: dict):
                pass

            async def connect(self) -> None:
                pass

            async def disconnect(self) -> None:
                pass

        driver = TestDriver()
        method = driver.on_camera_event

        assert method._is_event_subscription is True
        assert method._sub_device_id == "camera-001"
        assert method._sub_device_type is None
        assert method._sub_event_name is None

    def test_collect_event_subscriptions(self):
        """Test collecting all event subscriptions."""

        class TestDriver(DeviceDriver):
            device_type = "test"

            @property
            def identity(self) -> DeviceIdentity:
                return DeviceIdentity(device_type="test")

            @property
            def status(self) -> DeviceStatus:
                return DeviceStatus()

            @on(device_type="camera", event_name="motion")
            async def on_motion(self, device_id: str, event_name: str, payload: dict):
                pass

            @on(device_type="robot", event_name="complete")
            async def on_complete(self, device_id: str, event_name: str, payload: dict):
                pass

            async def connect(self) -> None:
                pass

            async def disconnect(self) -> None:
                pass

        driver = TestDriver()
        subscriptions = driver._collect_event_subscriptions()

        assert len(subscriptions) == 2
        assert any(s["event_name"] == "motion" for s in subscriptions)
        assert any(s["event_name"] == "complete" for s in subscriptions)


class TestDeviceDriverWithEvents:
    """Tests for agentic driver with event subscriptions."""

    def create_driver_with_subscription(self):
        """Create a driver with an event subscription."""
        received_events = []

        class TestDriver(DeviceDriver):
            device_type = "test_sub"

            @property
            def identity(self) -> DeviceIdentity:
                return DeviceIdentity(device_type="test_sub")

            @property
            def status(self) -> DeviceStatus:
                return DeviceStatus()

            @on(device_type="camera", event_name="motion")
            async def on_motion(self, device_id: str, event_name: str, payload: dict):
                received_events.append({
                    "device_id": device_id,
                    "event_name": event_name,
                    "payload": payload
                })

            async def connect(self) -> None:
                pass

            async def disconnect(self) -> None:
                pass

        driver = TestDriver()
        return driver, received_events

    @pytest.mark.asyncio
    async def test_setup_subscriptions_without_router(self):
        """Test setup_subscriptions logs warning without router."""
        driver, _ = self.create_driver_with_subscription()

        # Should not raise, just log warning
        await driver.setup_subscriptions()

        assert len(driver._subscriptions) == 0

    @pytest.mark.asyncio
    async def test_teardown_subscriptions(self):
        """Test teardown_subscriptions clears subscriptions."""
        driver, _ = self.create_driver_with_subscription()

        # Add mock subscription
        mock_sub = AsyncMock()
        driver._subscriptions.append(mock_sub)

        await driver.teardown_subscriptions()

        mock_sub.unsubscribe.assert_called_once()
        assert len(driver._subscriptions) == 0
