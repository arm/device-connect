"""Unit tests for device_connect_sdk.drivers module.

Tests @rpc, @emit decorators, schema generation, and DeviceDriver base class.
"""


import pytest

from device_connect_sdk.drivers import DeviceDriver, rpc, emit, build_function_schema, build_event_schema
from device_connect_sdk.types import DeviceIdentity, DeviceStatus


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
