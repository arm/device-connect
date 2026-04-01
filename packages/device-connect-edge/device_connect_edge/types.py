"""Core type definitions for Device Connect.

This module defines the fundamental data structures used throughout the
Device Connect framework, including device state, capabilities, identity,
and status models.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

from pydantic import BaseModel, Field


class DeviceState(str, Enum):
    """Device lifecycle states.

    Represents the various states a device can be in during its lifecycle,
    from initial provisioning through decommissioning.
    """
    PROVISIONED = "provisioned"        # Factory-provisioned, not yet commissioned
    COMMISSIONING = "commissioning"    # Awaiting admin commissioning
    REGISTERED = "registered"          # Connected and registered with registry
    ONLINE = "online"                  # Active and healthy
    OFFLINE = "offline"                # Disconnected (lease expired)
    MAINTENANCE = "maintenance"        # Under maintenance
    DECOMMISSIONED = "decommissioned"  # Retired from service


class FunctionDef(BaseModel):
    """Definition of a callable device function.

    Describes a function that a device exposes for remote invocation.
    The parameters field contains a JSON Schema describing the expected
    arguments.

    Example:
        FunctionDef(
            name="captureImage",
            description="Capture an image from the camera",
            parameters={
                "type": "object",
                "properties": {
                    "resolution": {"type": "string", "default": "1080p"},
                    "format": {"type": "string", "enum": ["jpeg", "png"]}
                },
                "required": []
            },
            tags=["vision", "capture"]
        )
    """
    name: str = Field(description="Function name (e.g., 'captureImage')")
    description: str = Field(default="", description="Human-readable description")
    parameters: Dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}, "required": []},
        description="JSON Schema for function parameters"
    )
    tags: List[str] = Field(
        default_factory=list,
        description="Tags for categorization (e.g., ['vision', 'capture'])"
    )


class EventDef(BaseModel):
    """Definition of a device event.

    Describes an event that a device can emit. Events are used to notify
    subscribers of state changes, detections, or other occurrences.

    Example:
        EventDef(
            name="event/objectDetected",
            description="Emitted when an object is detected in the camera frame",
            payload_schema={
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "confidence": {"type": "number"}
                }
            },
            tags=["vision", "detection"]
        )
    """
    name: str = Field(description="Event name (e.g., 'event/objectDetected')")
    description: str = Field(default="", description="Human-readable description")
    payload_schema: Optional[Dict[str, Any]] = Field(
        default=None,
        description="JSON Schema for event payload (optional)"
    )
    tags: List[str] = Field(
        default_factory=list,
        description="Tags for categorization"
    )


class DeviceCapabilities(BaseModel):
    """Device capabilities - functions and events the device exposes.

    This model describes what a device can do: which functions can be
    called and which events it can emit.

    Example:
        DeviceCapabilities(
            description="Security camera with object detection",
            functions=[
                FunctionDef(name="captureImage", description="Capture image"),
                FunctionDef(name="detectObjects", description="Run detection")
            ],
            events=[
                EventDef(name="event/objectDetected", description="Object found")
            ]
        )
    """
    description: str = Field(default="", description="Device description")
    functions: List[FunctionDef] = Field(
        default_factory=list,
        description="Functions exposed by the device"
    )
    events: List[EventDef] = Field(
        default_factory=list,
        description="Events the device can emit"
    )


class DeviceIdentity(BaseModel):
    """Immutable device identity and hardware information.

    Contains static information about the device that does not change
    during runtime. This is typically set at manufacturing or provisioning.

    Note: Device capabilities (what functions/events a device exposes) are
    defined via @rpc and @emit decorators on DeviceDriver
    methods, NOT here. This model is for hardware/identity metadata only.

    Arbitrary fields are allowed beyond the standard ones, enabling
    device-specific metadata (e.g., thermal_resolution, sensor_range).

    Example:
        DeviceIdentity(
            device_type="camera",
            manufacturer="Acme Corp",
            model="CameraPro-X1",
            serial_number="CAM-2024-001234",
            firmware_version="1.2.3",
            arch="arm64",
            description="Security camera with object detection capabilities",
            commissioning_comment="Ceiling camera overlooking Zone A entrance, 4K resolution",
            # Custom fields allowed:
            thermal_resolution="640x480",
            ir_mode=True,
        )
    """
    model_config = {"extra": "allow"}

    device_type: Optional[str] = Field(
        default=None,
        description="Device type (e.g., 'camera', 'robot', 'sensor')"
    )
    manufacturer: Optional[str] = Field(
        default=None,
        description="Device manufacturer"
    )
    model: Optional[str] = Field(
        default=None,
        description="Device model name/number"
    )
    serial_number: Optional[str] = Field(
        default=None,
        description="Unique serial number"
    )
    firmware_version: Optional[str] = Field(
        default=None,
        description="Firmware/software version"
    )
    arch: Optional[str] = Field(
        default=None,
        description="CPU architecture (e.g., 'arm64', 'x86_64')"
    )
    description: Optional[str] = Field(
        default=None,
        description="Human-readable description of the device for LLM understanding"
    )
    commissioning_comment: Optional[str] = Field(
        default=None,
        description="Admin-provided context during commissioning (zone, PoV, purpose). "
                    "Examples: 'Ceiling camera overlooking Zone A entrance', "
                    "'Mobile cleaning robot assigned to warehouse floor 2'"
    )


class DeviceStatus(BaseModel):
    """Runtime device status - dynamic state that changes over time.

    Contains information about the device's current operational state,
    which is updated via heartbeats and status changes.

    Example:
        DeviceStatus(
            ts=datetime.utcnow(),
            location="warehouse-A",
            availability="busy",
            busy_score=0.7,
            battery=85,
            online=True,
            error_state=None
        )
    """
    ts: datetime = Field(
        default_factory=datetime.utcnow,
        description="Timestamp of last status update"
    )
    location: Optional[str] = Field(
        default=None,
        description="Physical or logical location"
    )
    availability: str = Field(
        default="idle",
        description="Availability state (e.g., 'idle', 'busy', 'offline')"
    )
    busy_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Load indicator from 0.0 (idle) to 1.0 (fully busy)"
    )
    battery: Optional[int] = Field(
        default=None,
        ge=0,
        le=100,
        description="Battery level percentage (0-100) if applicable"
    )
    online: bool = Field(
        default=True,
        description="Whether the device is currently online and operational"
    )
    error_state: Optional[str] = Field(
        default=None,
        description="Error description if device is in error state"
    )


# Type aliases for callbacks
ConnectionCallback = Callable[[bool], Awaitable[None]]
"""Callback invoked when connection state changes. Parameter is True if connected."""

RegistrationCallback = Callable[[], Awaitable[None]]
"""Callback invoked when device registration completes."""
