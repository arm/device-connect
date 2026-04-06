"""Extended manifest schema for containerized capabilities.

Extends the standard Device Connect capability manifest.json with container
deployment configuration. When the ``container`` key is present, the capability
runs as a sidecar OCI container. When absent, the existing in-process
CapabilityLoader handles it.

Standard manifest fields (unchanged):
    id, class_name, entry_point, dependencies

Container-specific fields:
    container.image          — OCI image reference (built or pre-built)
    container.resources      — CPU/memory limits
    container.devices        — Host device pass-through (e.g. /dev/video0)
    container.shm_size       — Shared memory allocation for SHM transport
    container.ipc            — IPC backend: "zenoh-shm" (default) or "iceoryx2"
    container.realm          — Launch inside Arm CCA Realm (Phase 3)
    container.env            — Extra environment variables
    container.volumes        — Additional volume mounts
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ResourceLimits(BaseModel):
    """Container resource limits."""

    memory: str = Field(default="256Mi", description="Memory limit (e.g. '256Mi', '1Gi')")
    cpu: str = Field(default="0.5", description="CPU limit (e.g. '0.5', '2')")


class VolumeMount(BaseModel):
    """Container volume mount."""

    host_path: str = Field(description="Path on the host")
    container_path: str = Field(description="Path inside the container")
    read_only: bool = Field(default=False)


class ContainerConfig(BaseModel):
    """Container deployment configuration within a capability manifest.

    This is the ``container`` section of the manifest.json.
    """

    image: Optional[str] = Field(
        default=None,
        description="OCI image reference. If None, image is built from capability source.",
    )
    resources: ResourceLimits = Field(default_factory=ResourceLimits)
    devices: List[str] = Field(
        default_factory=list,
        description="Host devices to pass through (e.g. ['/dev/video0'])",
    )
    shm_size: str = Field(
        default="64Mi",
        description="Shared memory size for Zenoh SHM transport",
    )
    ipc: str = Field(
        default="zenoh-shm",
        description="IPC backend: 'zenoh-shm' or 'iceoryx2'",
    )
    realm: bool = Field(
        default=False,
        description="Launch inside Arm CCA Realm (requires Kata + CoCo)",
    )
    env: Dict[str, str] = Field(
        default_factory=dict,
        description="Extra environment variables for the container",
    )
    volumes: List[VolumeMount] = Field(
        default_factory=list,
        description="Additional volume mounts",
    )
    build_args: Dict[str, str] = Field(
        default_factory=dict,
        description="Docker build arguments (e.g. ENABLE_MTE=true)",
    )


class ContainerManifest(BaseModel):
    """Full capability manifest with optional container configuration.

    Superset of the standard manifest.json format. The ``container`` field
    is the opt-in trigger for containerized deployment.
    """

    id: str = Field(description="Capability identifier")
    class_name: str = Field(description="Python class name implementing the capability")
    entry_point: str = Field(default="capability.py", description="Python source file")
    description: str = Field(default="", description="Human-readable description")
    dependencies: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Declared dependencies (e.g. {'python': ['opencv-python>=4.8']})",
    )
    container: Optional[ContainerConfig] = Field(
        default=None,
        description="Container deployment config. If None, capability loads in-process.",
    )

    @property
    def is_containerized(self) -> bool:
        """Whether this capability should run as a container sidecar."""
        return self.container is not None

    @classmethod
    def from_manifest_file(cls, manifest_path: Path) -> "ContainerManifest":
        """Load manifest from a JSON file.

        Args:
            manifest_path: Path to manifest.json

        Returns:
            Parsed ContainerManifest
        """
        with open(manifest_path) as f:
            data = json.load(f)
        return cls(**data)

    @classmethod
    def from_capability_dir(cls, cap_dir: Path) -> "ContainerManifest":
        """Load manifest from a capability directory.

        Args:
            cap_dir: Directory containing manifest.json

        Returns:
            Parsed ContainerManifest

        Raises:
            FileNotFoundError: If manifest.json not found
        """
        manifest_path = cap_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"No manifest.json in {cap_dir}")
        return cls.from_manifest_file(manifest_path)
