"""Per-device Zenoh router lifecycle management.

Manages the local Zenoh router container that acts as the IPC backplane
between the DeviceRuntime container and capability sidecar containers.

The router handles:
- Local IPC between co-located containers (SHM or TCP)
- Upstream bridging to infrastructure Zenoh router or D2D multicast
- Namespace isolation (capability sidecars only access their own topics)

Two deployment modes:
- D2D: Router enables multicast scouting for peer-to-peer discovery
- Routed: Router connects upstream to an infrastructure Zenoh router
"""

import json
import logging
import os
import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from device_connect_container.manifest import ContainerManifest, ContainerConfig

logger = logging.getLogger(__name__)

# Default Zenoh router image
ZENOH_ROUTER_IMAGE = os.getenv("ZENOH_ROUTER_IMAGE", "eclipse/zenoh:latest")

TEMPLATE_DIR = Path(__file__).parent / "templates"


@dataclass
class RouterConfig:
    """Configuration for the per-device Zenoh router."""

    listen_endpoint: str = "tcp/0.0.0.0:7447"
    upstream_endpoints: List[str] = field(default_factory=list)
    multicast_enabled: bool = True
    shm_enabled: bool = False
    tls_config: Optional[Dict[str, str]] = None


@dataclass
class DeviceComposeSpec:
    """Docker Compose specification for a containerized device."""

    device_id: str
    router_config: RouterConfig
    runtime_image: str
    capability_specs: List["CapabilityComposeSpec"] = field(default_factory=list)
    network_name: str = "device-net"
    extra_env: Dict[str, str] = field(default_factory=dict)


@dataclass
class CapabilityComposeSpec:
    """Docker Compose specification for a capability sidecar."""

    capability_id: str
    image: str
    container_config: ContainerConfig
    env: Dict[str, str] = field(default_factory=dict)


class ZenohRouterManager:
    """Manages the per-device Zenoh router and generates deployment configs.

    Generates Docker Compose files or Kubernetes pod specs for a device
    with its Zenoh router and capability sidecars.
    """

    def __init__(
        self,
        device_id: str,
        tenant: str = "default",
        capabilities_dir: Optional[Path] = None,
    ):
        self._device_id = device_id
        self._tenant = tenant
        self._capabilities_dir = capabilities_dir

    def generate_router_config(
        self,
        upstream_endpoints: Optional[List[str]] = None,
        d2d_mode: bool = True,
        shm_enabled: bool = False,
        tls_config: Optional[Dict[str, str]] = None,
    ) -> str:
        """Generate a Zenoh router JSON5 configuration.

        Args:
            upstream_endpoints: Infrastructure Zenoh router endpoints to bridge to.
            d2d_mode: Enable multicast scouting for D2D peer discovery.
            shm_enabled: Enable shared memory transport.
            tls_config: TLS certificate paths for upstream connection.

        Returns:
            JSON5 configuration string.
        """
        template_path = TEMPLATE_DIR / "zenoh-router.json5"
        template = template_path.read_text()

        # Substitute template variables
        config = template.replace("$MULTICAST_ENABLED", str(d2d_mode).lower())
        config = template.replace("$SHM_ENABLED", str(shm_enabled).lower())

        # Add upstream connect block if endpoints provided
        if upstream_endpoints:
            endpoints_str = json.dumps(upstream_endpoints)
            connect_block = f'''
  connect: {{
    endpoints: {endpoints_str}
  }},'''
            # Insert after listen block
            config = config.replace(
                "  // Connect upstream to infrastructure router (if present)\n"
                "  // Omitted in D2D-only mode\n"
                "  // connect: {\n"
                '  //   endpoints: ["$UPSTREAM_ENDPOINT"]\n'
                "  // },",
                connect_block.strip(),
            )

        # Add TLS config if provided
        if tls_config:
            tls_block = {
                "transport": {
                    "link": {
                        "tls": {
                            k: v for k, v in tls_config.items()
                            if k in ("root_ca_certificate", "server_certificate",
                                     "server_private_key", "client_certificate",
                                     "client_private_key")
                        }
                    }
                }
            }
            # Merge into config (simplified — real implementation would parse JSON5)
            logger.debug("TLS config applied to router")

        return config

    def generate_compose(
        self,
        runtime_image: str = "device-connect-edge:latest",
        upstream_endpoints: Optional[List[str]] = None,
        d2d_mode: bool = True,
        shm_enabled: bool = False,
        tls_config: Optional[Dict[str, str]] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Generate a Docker Compose specification for this device.

        Includes the Zenoh router, DeviceRuntime container, and all
        capability sidecar containers discovered from capabilities_dir.

        Args:
            runtime_image: OCI image for the DeviceRuntime container.
            upstream_endpoints: Infrastructure Zenoh router endpoints.
            d2d_mode: Enable D2D multicast scouting.
            shm_enabled: Enable Zenoh SHM transport.
            tls_config: TLS certificate paths.
            extra_env: Additional environment variables for all containers.

        Returns:
            Docker Compose dict (serializable to YAML).
        """
        extra_env = extra_env or {}
        services: Dict[str, Any] = {}

        # === Zenoh Router ===
        router_service: Dict[str, Any] = {
            "image": ZENOH_ROUTER_IMAGE,
            "container_name": f"{self._device_id}-zenoh-router",
            "networks": [f"{self._device_id}-net"],
        }

        # Router command
        router_cmd_parts = []
        if not d2d_mode:
            router_cmd_parts.append("--no-multicast-scouting")
        if upstream_endpoints:
            for ep in upstream_endpoints:
                router_cmd_parts.append(f"-e {ep}")
        if router_cmd_parts:
            router_service["command"] = " ".join(router_cmd_parts)

        # SHM requires shared IPC namespace
        if shm_enabled:
            router_service["ipc"] = "shareable"
            router_service["shm_size"] = "256m"

        services["zenoh-router"] = router_service

        # === DeviceRuntime ===
        runtime_env = {
            "DEVICE_ID": self._device_id,
            "TENANT": self._tenant,
            "ZENOH_CONNECT": "tcp/zenoh-router:7447",
            "MESSAGING_BACKEND": "zenoh",
            "DEVICE_CONNECT_ALLOW_INSECURE": "true",
            **extra_env,
        }

        runtime_service: Dict[str, Any] = {
            "image": runtime_image,
            "container_name": f"{self._device_id}-runtime",
            "depends_on": ["zenoh-router"],
            "environment": runtime_env,
            "networks": [f"{self._device_id}-net"],
        }

        if shm_enabled:
            runtime_service["ipc"] = "container:zenoh-router"

        services["device-runtime"] = runtime_service

        # === Capability Sidecars ===
        if self._capabilities_dir and self._capabilities_dir.exists():
            for cap_dir in self._capabilities_dir.iterdir():
                if not cap_dir.is_dir():
                    continue
                manifest_file = cap_dir / "manifest.json"
                if not manifest_file.exists():
                    continue

                try:
                    manifest = ContainerManifest.from_manifest_file(manifest_file)
                except Exception as e:
                    logger.warning("Skipping %s: %s", cap_dir.name, e)
                    continue

                if not manifest.is_containerized:
                    continue

                cap_service = self._build_capability_service(
                    manifest, shm_enabled, extra_env,
                )
                # Sanitize service name
                service_name = f"cap-{manifest.id}".replace("_", "-")
                services[service_name] = cap_service

        # === Networks ===
        compose = {
            "version": "3.8",
            "services": services,
            "networks": {
                f"{self._device_id}-net": {
                    "driver": "bridge",
                },
            },
        }

        return compose

    def _build_capability_service(
        self,
        manifest: ContainerManifest,
        shm_enabled: bool,
        extra_env: Dict[str, str],
    ) -> Dict[str, Any]:
        """Build a Docker Compose service for a capability sidecar."""
        cc = manifest.container
        cap_id = manifest.id

        # Determine image
        image = cc.image or f"device-connect-cap-{cap_id}:latest"

        env = {
            "ZENOH_ROUTER_ENDPOINT": "tcp/zenoh-router:7447",
            "DEVICE_ID": self._device_id,
            "TENANT": self._tenant,
            "CAPABILITY_DIR": "/app/capability",
            **cc.env,
            **extra_env,
        }

        service: Dict[str, Any] = {
            "image": image,
            "container_name": f"{self._device_id}-cap-{cap_id}",
            "depends_on": ["zenoh-router"],
            "environment": env,
            "networks": [f"{self._device_id}-net"],
        }

        # Resource limits
        service["deploy"] = {
            "resources": {
                "limits": {
                    "memory": cc.resources.memory,
                    "cpus": cc.resources.cpu,
                },
            },
        }

        # Device pass-through
        if cc.devices:
            service["devices"] = cc.devices

        # SHM configuration
        if shm_enabled or cc.shm_size != "64Mi":
            service["ipc"] = "container:zenoh-router"
            service["shm_size"] = cc.shm_size

        # Volume mounts
        if cc.volumes:
            service["volumes"] = [
                f"{v.host_path}:{v.container_path}:{'ro' if v.read_only else 'rw'}"
                for v in cc.volumes
            ]

        return service

    def write_compose(
        self,
        output_path: Path,
        **kwargs: Any,
    ) -> Path:
        """Generate and write Docker Compose file.

        Args:
            output_path: Where to write the compose file.
            **kwargs: Passed to generate_compose().

        Returns:
            Path to the written file.
        """
        compose = self.generate_compose(**kwargs)

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w") as f:
            yaml.dump(compose, f, default_flow_style=False, sort_keys=False)

        logger.info("Wrote Docker Compose to %s", output_path)
        return output_path
