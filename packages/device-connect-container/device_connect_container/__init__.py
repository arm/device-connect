"""Device Connect Container — containerized capability deployment.

Provides infrastructure for running Device Connect capabilities as OCI
containers with per-device Zenoh router backplane, SHM zero-copy IPC,
and Armv9 hardware security features.

Phase 1: Container sidecar model (ContainerCapabilityLoader, SidecarRuntime)
Phase 2: SHM + FlatBuffers (ShmChannel, @stream decorator)
Phase 3: Armv9 security (attestation, Realms, MTE, image signing)
"""

__version__ = "0.1.0"

from device_connect_container.manifest import ContainerManifest, ContainerConfig
from device_connect_container.container_loader import (
    ContainerCapabilityLoader,
    ContainerCapabilityProxy,
)
from device_connect_container.sidecar_runtime import CapabilitySidecarRuntime
from device_connect_container.zenoh_router import ZenohRouterManager

__all__ = [
    "ContainerManifest",
    "ContainerConfig",
    "ContainerCapabilityLoader",
    "ContainerCapabilityProxy",
    "CapabilitySidecarRuntime",
    "ZenohRouterManager",
]
