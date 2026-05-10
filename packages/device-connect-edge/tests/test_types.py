# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for device_connect_edge.types module."""

from device_connect_edge.types import (
    DeviceState,
    DeviceIdentity,
    DeviceStatus,
    FunctionDef,
    EventDef,
    DeviceCapabilities,
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


class TestLabels:
    """Discovery labels on FunctionDef, EventDef, DeviceCapabilities (Phase 1)."""

    def test_function_labels_default_none(self):
        f = FunctionDef(name="ping")
        assert f.labels is None

    def test_function_single_value_label(self):
        f = FunctionDef(name="get_status", labels={"direction": "read"})
        assert f.labels == {"direction": "read"}

    def test_function_multivalued_label(self):
        f = FunctionDef(name="capture", labels={"modality": ["rgb", "4k"]})
        assert f.labels == {"modality": ["rgb", "4k"]}

    def test_function_labels_roundtrip(self):
        f = FunctionDef(
            name="set_threshold",
            labels={"direction": "write", "modality": ["rgb", "4k"], "safety": "critical"},
        )
        f2 = FunctionDef.model_validate_json(f.model_dump_json())
        assert f2.labels == f.labels

    def test_event_labels_default_none(self):
        e = EventDef(name="heartbeat")
        assert e.labels is None

    def test_event_labels_roundtrip(self):
        e = EventDef(
            name="motion_detected",
            labels={"modality": "motion", "safety": "informational"},
        )
        e2 = EventDef.model_validate_json(e.model_dump_json())
        assert e2.labels == e.labels

    def test_capabilities_labels_default_none(self):
        c = DeviceCapabilities()
        assert c.labels is None

    def test_capabilities_labels_composite_identity(self):
        # category multi-valued for composite devices (camera + inference)
        c = DeviceCapabilities(
            labels={
                "category": ["camera", "inference"],
                "location": "warehouse1/loading-dock",
            }
        )
        assert c.labels["category"] == ["camera", "inference"]
        assert c.labels["location"] == "warehouse1/loading-dock"

    def test_capabilities_labels_roundtrip(self):
        c = DeviceCapabilities(
            description="Smart cam",
            functions=[FunctionDef(name="capture", labels={"direction": "write"})],
            events=[EventDef(name="motion", labels={"modality": "motion"})],
            labels={"category": ["camera"], "location": "warehouse1/dock-3"},
        )
        c2 = DeviceCapabilities.model_validate_json(c.model_dump_json())
        assert c2.labels == c.labels
        assert c2.functions[0].labels == {"direction": "write"}
        assert c2.events[0].labels == {"modality": "motion"}
