"""OCI image builder for capability containers.

Builds Docker images from capability directories using the standard
Dockerfile template. Can also generate Dockerfiles for custom builds.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from device_connect_container.manifest import ContainerManifest

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent.parent / "templates"


class ImageBuilder:
    """Build OCI container images for Device Connect capabilities.

    Takes a capability directory (same layout as CapabilityLoader scans)
    and produces an OCI image using the template Dockerfile.
    """

    def __init__(
        self,
        registry: str = "",
        tag_prefix: str = "device-connect-cap",
        platform: str = "linux/arm64",
    ):
        """
        Args:
            registry: OCI registry prefix (e.g. "ghcr.io/arm/").
            tag_prefix: Image name prefix.
            platform: Target platform for multi-arch builds.
        """
        self._registry = registry.rstrip("/") + "/" if registry else ""
        self._tag_prefix = tag_prefix
        self._platform = platform

    def build_image(
        self,
        capability_dir: Path,
        tag: Optional[str] = None,
        build_args: Optional[Dict[str, str]] = None,
        no_cache: bool = False,
    ) -> str:
        """Build an OCI image for a capability.

        Args:
            capability_dir: Path to capability directory with manifest.json.
            tag: Explicit image tag. If None, auto-generated from manifest.
            build_args: Additional Docker build arguments.
            no_cache: Disable Docker build cache.

        Returns:
            Full image name:tag string.
        """
        manifest = ContainerManifest.from_capability_dir(capability_dir)
        build_args = build_args or {}

        # Merge build args from manifest
        if manifest.container and manifest.container.build_args:
            merged_args = dict(manifest.container.build_args)
            merged_args.update(build_args)
            build_args = merged_args

        # Determine image tag
        if tag is None:
            if manifest.container and manifest.container.image:
                image_tag = manifest.container.image
            else:
                image_tag = f"{self._registry}{self._tag_prefix}-{manifest.id}:latest"
        else:
            image_tag = tag

        # Collect Python dependencies for PIP_DEPS build arg
        pip_deps = manifest.dependencies.get("python", [])
        if pip_deps:
            build_args.setdefault("PIP_DEPS", " ".join(pip_deps))

        # Generate Dockerfile in a temp build context
        dockerfile_template = (TEMPLATE_DIR / "Dockerfile.capability").read_text()

        # Build using docker
        cmd = [
            "docker", "build",
            "-t", image_tag,
            "-f", "-",  # Dockerfile from stdin
        ]

        if self._platform:
            cmd.extend(["--platform", self._platform])

        if no_cache:
            cmd.append("--no-cache")

        for key, value in build_args.items():
            cmd.extend(["--build-arg", f"{key}={value}"])

        # Build context is the capability directory
        cmd.append(str(capability_dir))

        logger.info("Building image: %s from %s", image_tag, capability_dir)
        result = subprocess.run(
            cmd,
            input=dockerfile_template.encode(),
            capture_output=True,
        )

        if result.returncode != 0:
            stderr = result.stderr.decode()
            raise RuntimeError(f"Docker build failed: {stderr}")

        logger.info("Built image: %s", image_tag)
        return image_tag

    def build_all(
        self,
        capabilities_dir: Path,
        no_cache: bool = False,
    ) -> List[str]:
        """Build images for all containerized capabilities in a directory.

        Args:
            capabilities_dir: Parent directory containing capability subdirs.
            no_cache: Disable Docker build cache.

        Returns:
            List of built image name:tag strings.
        """
        built = []

        for cap_dir in capabilities_dir.iterdir():
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
                logger.debug("Skipping non-containerized: %s", manifest.id)
                continue

            try:
                image_tag = self.build_image(cap_dir, no_cache=no_cache)
                built.append(image_tag)
            except Exception as e:
                logger.error("Failed to build %s: %s", manifest.id, e)

        return built

    def generate_dockerfile(
        self,
        capability_dir: Path,
        output_path: Optional[Path] = None,
    ) -> str:
        """Generate a Dockerfile for a capability (without building).

        Args:
            capability_dir: Path to capability directory.
            output_path: Where to write the Dockerfile. If None, returns content.

        Returns:
            Dockerfile content.
        """
        manifest = ContainerManifest.from_capability_dir(capability_dir)
        content = (TEMPLATE_DIR / "Dockerfile.capability").read_text()

        if output_path:
            output_path.write_text(content)
            logger.info("Generated Dockerfile at %s", output_path)

        return content
