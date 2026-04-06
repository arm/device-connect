"""Device driver framework for Device Connect.

This module provides the foundation for implementing device-specific logic.
Device drivers define what functions a device exposes and how to invoke them.

Key Components:
    - DeviceDriver: Base class for device drivers with D2D communication built-in
    - @rpc: Decorator to mark methods as RPC-callable functions
    - @emit: Decorator to declare emittable events
    - @before_emit: Decorator to intercept own events before pubsub
    - @periodic: Decorator for auto-managed periodic tasks
    - @on: Decorator to subscribe to events from other devices

Example:
    from device_connect_edge.drivers import DeviceDriver, rpc, emit

    class CameraDriver(DeviceDriver):
        device_type = "camera"

        @rpc()
        async def capture_image(self, resolution: str = "1080p") -> dict:
            '''Capture an image from the camera.'''
            return {"image_b64": "..."}

        @emit()
        async def motion_detected(self, zone: str, confidence: float):
            '''Motion detected in camera view.'''
            pass
"""
from device_connect_edge.drivers.base import DeviceDriver, on
from device_connect_edge.drivers.decorators import (
    rpc,
    emit,
    before_emit,
    periodic,
    build_function_schema,
    build_event_schema,
)
from device_connect_edge.drivers.transport import DriverTransport
from device_connect_edge.drivers.capability_loader import (
    CapabilityLoader,
    CapabilityDriverMixin,
    LoadedCapability,
    EventSubscription,
)

__all__ = [
    "DeviceDriver",
    "DriverTransport",
    "rpc",
    "emit",
    "before_emit",
    "periodic",
    "on",
    "build_function_schema",
    "build_event_schema",
    # Capability loading
    "CapabilityLoader",
    "CapabilityDriverMixin",
    "LoadedCapability",
    "EventSubscription",
]
