"""Device Connect Edge — lightweight runtime for edge devices.

Build IoT devices with Python. Connect them over Zenoh or NATS. Communicate
device-to-device using RPC and events. This is the only package a
Raspberry Pi (or any edge device) needs to install.

Example:
    from device_connect_edge import DeviceRuntime
    from device_connect_edge.drivers import DeviceDriver, rpc, emit

    class Sensor(DeviceDriver):
        device_type = "sensor"

        @rpc()
        async def get_reading(self) -> dict:
            return {"temp": 22.5}

        @emit()
        async def alert(self, level: str, msg: str):
            pass

    device = DeviceRuntime(
        driver=Sensor(),
        device_id="sensor-001",
        messaging_urls=["tcp/localhost:7447"],
    )
    await device.run()
"""
from device_connect_edge.device import (
    DeviceRuntime,
    build_rpc_error,
    build_rpc_response,
)
from device_connect_edge.types import (
    DeviceState,
    DeviceCapabilities,
    DeviceIdentity,
    DeviceStatus,
    FunctionDef,
    EventDef,
)
from device_connect_edge.discovery_provider import DiscoveryProvider
from device_connect_edge.registry_client import RegistryClient
from device_connect_edge.errors import (
    DeviceConnectError,
    DeviceError,
    DeviceDependencyError,
    DeviceConnectionError,
    RegistrationError,
    FunctionInvocationError,
    ValidationError,
    CommissioningError,
)

__all__ = [
    "DeviceRuntime",
    "build_rpc_error",
    "build_rpc_response",
    "DeviceState",
    "DeviceCapabilities",
    "DeviceIdentity",
    "DeviceStatus",
    "FunctionDef",
    "EventDef",
    "DiscoveryProvider",
    "RegistryClient",
    "DeviceConnectError",
    "DeviceError",
    "DeviceDependencyError",
    "DeviceConnectionError",
    "RegistrationError",
    "FunctionInvocationError",
    "ValidationError",
    "CommissioningError",
]
