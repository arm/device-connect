"""Device Connect - Edge device orchestration framework.

This package re-exports everything from device_connect_sdk (the lightweight
device SDK) and adds core extensions: registry, security, state, logging,
CapabilityLoader, devctl, and statectl.

Core Components:
    - DeviceRuntime: Runtime that hosts a DeviceDriver (handles messaging, registration, heartbeats)
    - DeviceDriver: Abstract base for device-specific logic
    - DeviceCapabilities, DeviceIdentity, DeviceStatus: Type definitions

Submodules:
    - device_connect_server.drivers: Device driver framework with decorators
    - device_connect_server.messaging: Pluggable messaging (NATS, MQTT)
    - device_connect_server.security: ACLs, commissioning, credentials
    - device_connect_server.state: State store abstractions
    - device_connect_server.registry: Registry client and service
    - device_connect_server.telemetry: OpenTelemetry distributed tracing
    - device_connect_server.logging: Audit logging framework
    - device_connect_server.devctl: Device control CLI
    - device_connect_server.statectl: State management CLI

Example:
    from device_connect_server import DeviceRuntime
    from device_connect_server.drivers import DeviceDriver, rpc
    from device_connect_server.types import DeviceCapabilities

    class CameraDriver(DeviceDriver):
        device_type = "camera"

        @rpc()
        async def capture_image(self, resolution: str = "1080p") -> dict:
            '''Capture an image.'''
            return {"image_b64": "..."}

    device = DeviceRuntime(
        driver=CameraDriver(),
        device_id="camera-001",
        messaging_urls=["nats://localhost:4222"]
    )
    await device.run()
"""
# Re-export everything from device_connect_sdk
from device_connect_sdk import (  # noqa: F401
    DeviceRuntime,
    build_rpc_error,
    build_rpc_response,
    DeviceState,
    DeviceCapabilities,
    DeviceIdentity,
    DeviceStatus,
    FunctionDef,
    EventDef,
    DeviceConnectError,
    DeviceError,
    RegistrationError,
    FunctionInvocationError,
    ValidationError,
    CommissioningError,
)

__all__ = [
    # Device runtime
    "DeviceRuntime",
    "build_rpc_error",
    "build_rpc_response",
    # Types
    "DeviceState",
    "DeviceCapabilities",
    "DeviceIdentity",
    "DeviceStatus",
    "FunctionDef",
    "EventDef",
    # Errors
    "DeviceConnectError",
    "DeviceError",
    "RegistrationError",
    "FunctionInvocationError",
    "ValidationError",
    "CommissioningError",
]
