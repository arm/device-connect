"""Device driver framework for Device Connect.

Re-exports from device_connect_edge.drivers and adds core-only extensions:
    - CapabilityLoader: Runtime loading of device capabilities from disk
"""
# Re-export everything from device_connect_edge.drivers
from device_connect_edge.drivers import (  # noqa: F401
    DeviceDriver,
    on,
    rpc,
    emit,
    before_emit,
    periodic,
    build_function_schema,
    build_event_schema,
)

# Core-only extensions
from device_connect_server.drivers.capability_loader import (  # noqa: F401
    CapabilityLoader,
    CapabilityDriverMixin,
    LoadedCapability,
    EventSubscription,
)

__all__ = [
    "DeviceDriver",
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
