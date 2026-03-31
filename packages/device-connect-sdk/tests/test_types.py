"""Unit tests for device_connect_sdk.types module."""

from device_connect_sdk.types import (
    DeviceState,
    DeviceIdentity,
    DeviceStatus,
    FunctionDef,
    EventDef,
)


class TestDeviceState:
    def test_enum_values(self):
        assert DeviceState.REGISTERED == "registered"
        assert DeviceState.ONLINE == "online"
        assert DeviceState.OFFLINE == "offline"

    def test_enum_from_string(self):
        assert DeviceState("registered") == DeviceState.REGISTERED


class TestDeviceIdentity:
    def test_create(self):
        identity = DeviceIdentity(
            device_type="camera",
            manufacturer="TestCorp",
            model="Cam-1000",
            firmware_version="1.0.0",
        )
        assert identity.device_type == "camera"
        assert identity.manufacturer == "TestCorp"

    def test_optional_fields(self):
        identity = DeviceIdentity(
            device_type="sensor",
            manufacturer="Test",
            model="S1",
            firmware_version="0.1",
        )
        # Optional fields should have defaults
        assert identity.device_type == "sensor"


class TestDeviceStatus:
    def test_create(self):
        status = DeviceStatus(location="lab-A")
        assert status.location == "lab-A"

    def test_defaults(self):
        status = DeviceStatus()
        # Should have sensible defaults
        assert status is not None


class TestFunctionDef:
    def test_create(self):
        func = FunctionDef(
            name="capture_image",
            description="Capture an image",
            parameters={"type": "object", "properties": {"resolution": {"type": "string"}}},
        )
        assert func.name == "capture_image"
        assert func.description == "Capture an image"


class TestEventDef:
    def test_create(self):
        event = EventDef(
            name="motion_detected",
            description="Motion detected",
            parameters={"type": "object", "properties": {"zone": {"type": "string"}}},
        )
        assert event.name == "motion_detected"
