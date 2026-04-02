"""Unit tests for device_connect_edge.drivers module.

Tests @rpc, @emit, @on decorators, schema generation, and DeviceDriver base class.
"""


import pytest
from unittest.mock import AsyncMock, MagicMock

from device_connect_edge.drivers import DeviceDriver, rpc, emit, build_function_schema, build_event_schema
from device_connect_edge.drivers.base import on
from device_connect_edge.types import DeviceIdentity, DeviceStatus


# ── @rpc decorator ─────────────────────────────────────────────────

class TestRpc:
    def test_marks_function(self):
        @rpc()
        async def my_func(self, x: int) -> dict:
            """Do something."""
            return {"x": x}

        assert my_func._is_device_function is True
        assert my_func._function_name == "my_func"

    def test_custom_name(self):
        @rpc(name="customName")
        async def my_func(self) -> dict:
            """A function."""
            return {}

        assert my_func._function_name == "customName"

    def test_description_from_docstring(self):
        @rpc()
        async def capture(self, resolution: str = "1080p") -> dict:
            """Capture an image from the camera.

            Args:
                resolution: Image resolution
            """
            return {}

        assert capture._description == "Capture an image from the camera."

    def test_custom_description(self):
        @rpc(description="Custom desc")
        async def func(self) -> dict:
            """Original."""
            return {}

        assert func._description == "Custom desc"


# ── @emit decorator ────────────────────────────────────────────────

class TestEmit:
    def test_marks_event(self):
        @emit("object_detected")
        async def object_detected(self, label: str):
            """Object detected."""
            pass

        assert object_detected._is_device_event is True
        assert object_detected._event_name == "object_detected"

    def test_event_description(self):
        @emit("motion_detected")
        async def motion_detected(self, zone: str):
            """Motion detected in zone.

            Args:
                zone: Zone identifier
            """
            pass

        assert motion_detected._event_description == "Motion detected in zone."

    def test_inferred_event_name(self):
        @emit()
        async def alert_triggered(self, level: str):
            """Alert triggered."""
            pass

        assert alert_triggered._event_name == "alert_triggered"


# ── Schema generation ──────────────────────────────────────────────

class TestBuildFunctionSchema:
    def test_simple_schema(self):
        @rpc()
        async def func(self, name: str, count: int = 10) -> dict:
            """A function."""
            return {}

        schema = build_function_schema(func)
        # Schema is a JSON Schema object with properties at top level
        assert "properties" in schema
        assert "name" in schema["properties"]
        assert "count" in schema["properties"]

    def test_no_params(self):
        @rpc()
        async def ping(self) -> dict:
            """Ping."""
            return {}

        schema = build_function_schema(ping)
        assert schema["type"] == "object"


class TestBuildEventSchema:
    def test_event_schema(self):
        @emit("reading")
        async def reading(self, temperature: float, humidity: float):
            """Sensor reading."""
            pass

        schema = build_event_schema(reading)
        assert "properties" in schema
        assert "temperature" in schema["properties"]
        assert "humidity" in schema["properties"]


# ── DeviceDriver base class ───────────────────────────────────────

class SampleDriver(DeviceDriver):
    device_type = "sample"

    @property
    def identity(self) -> DeviceIdentity:
        return DeviceIdentity(device_type="sample", manufacturer="Test", model="S1", firmware_version="0.1")

    @property
    def status(self) -> DeviceStatus:
        return DeviceStatus(location="lab")

    @rpc()
    async def do_something(self, value: int) -> dict:
        """Do something."""
        return {"result": value * 2}

    @emit()
    async def something_happened(self, detail: str):
        """Something happened."""
        pass

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass


class TestDeviceDriverBase:
    def test_identity(self):
        driver = SampleDriver()
        assert driver.identity.device_type == "sample"

    def test_status(self):
        driver = SampleDriver()
        assert driver.status.location == "lab"

    def test_device_type(self):
        driver = SampleDriver()
        assert driver.device_type == "sample"

    @pytest.mark.asyncio
    async def test_rpc_callable(self):
        driver = SampleDriver()
        result = await driver.do_something(value=5)
        assert result == {"result": 10}

    def test_capabilities_detected(self):
        """Driver should have functions and events detectable via introspection."""
        driver = SampleDriver()
        # Check that decorated methods are discoverable
        funcs = [
            m for m in dir(driver)
            if getattr(getattr(driver, m, None), "_is_device_function", False)
        ]
        events = [
            m for m in dir(driver)
            if getattr(getattr(driver, m, None), "_is_device_event", False)
        ]
        # At least our decorated methods should be found
        func_names = [getattr(getattr(driver, m), "_function_name") for m in funcs]
        event_names = [getattr(getattr(driver, m), "_event_name") for m in events]
        assert "do_something" in func_names
        assert "something_happened" in event_names


# ── @on decorator ─────────────────────────────────────────────────

class TestOn:
    def test_marks_event_subscription(self):
        @on(device_type="camera", event_name="motion_detected")
        async def handler(self, device_id, event_name, payload):
            pass

        assert handler._is_event_subscription is True
        assert handler._sub_device_type == "camera"
        assert handler._sub_event_name == "motion_detected"
        assert handler._sub_device_id is None

    def test_with_device_id(self):
        @on(device_id="cam-001", event_name="alert")
        async def handler(self, device_id, event_name, payload):
            pass

        assert handler._sub_device_id == "cam-001"
        assert handler._sub_device_type is None

    def test_all_params(self):
        @on(device_id="robot-1", device_type="robot", event_name="done")
        async def handler(self, device_id, event_name, payload):
            pass

        assert handler._sub_device_id == "robot-1"
        assert handler._sub_device_type == "robot"
        assert handler._sub_event_name == "done"

    def test_defaults_to_none(self):
        @on()
        async def handler(self, device_id, event_name, payload):
            pass

        assert handler._is_event_subscription is True
        assert handler._sub_device_id is None
        assert handler._sub_device_type is None
        assert handler._sub_event_name is None


# ── _collect_event_subscriptions ──────────────────────────────────

class TestCollectEventSubscriptions:
    def test_collects_on_methods(self):
        class MyDriver(DeviceDriver):
            device_type = "test"

            @on(device_type="camera", event_name="motion")
            async def on_motion(self, device_id, event_name, payload):
                pass

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        driver = MyDriver()
        subs = driver._collect_event_subscriptions()
        assert len(subs) == 1
        assert subs[0]["device_type"] == "camera"
        assert subs[0]["event_name"] == "motion"

    def test_ignores_rpc_methods(self):
        class MyDriver(DeviceDriver):
            device_type = "test"

            @rpc()
            async def do_thing(self) -> dict:
                """A function."""
                return {}

            @on(device_type="sensor")
            async def on_reading(self, device_id, event_name, payload):
                pass

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        driver = MyDriver()
        subs = driver._collect_event_subscriptions()
        assert len(subs) == 1
        assert subs[0]["device_type"] == "sensor"

    def test_multiple_subscriptions(self):
        class MyDriver(DeviceDriver):
            device_type = "test"

            @on(device_type="camera", event_name="motion")
            async def on_motion(self, device_id, event_name, payload):
                pass

            @on(device_id="robot-001", event_name="done")
            async def on_done(self, device_id, event_name, payload):
                pass

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        driver = MyDriver()
        subs = driver._collect_event_subscriptions()
        assert len(subs) == 2


# ── setup_subscriptions error isolation ───────────────────────────

class TestSetupSubscriptionsErrorIsolation:
    @pytest.mark.asyncio
    async def test_one_failure_does_not_block_others(self):
        """If one subscription fails, the rest should still be set up."""

        class MyDriver(DeviceDriver):
            device_type = "test"

            @on(device_type="camera", event_name="motion")
            async def on_motion(self, device_id, event_name, payload):
                pass

            @on(device_type="sensor", event_name="reading")
            async def on_reading(self, device_id, event_name, payload):
                pass

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        driver = MyDriver()

        # Use a simple object (not MagicMock) to avoid MagicMock's
        # auto-attribute creation leaking into DeviceDriver introspection.
        mock_messaging = AsyncMock()
        call_count = 0

        async def fail_then_succeed(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("first subscription fails")
            return MagicMock()  # subscription handle

        mock_messaging.subscribe_with_subject = AsyncMock(side_effect=fail_then_succeed)

        class FakeRouter:
            def __init__(self):
                self._messaging = mock_messaging
                self._tenant = "default"

        driver._router = FakeRouter()

        await driver.setup_subscriptions()

        # Both were attempted
        assert mock_messaging.subscribe_with_subject.await_count == 2
        # Only the second (successful) subscription was tracked
        assert len(driver._subscriptions) == 1
