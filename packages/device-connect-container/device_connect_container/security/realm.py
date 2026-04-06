"""Arm CCA Realm lifecycle management for confidential capability containers.

Manages the lifecycle of Arm Confidential Computing Architecture (CCA)
Realms for running sensitive capabilities in hardware-encrypted enclaves.

A Realm is an isolated execution environment where even the hypervisor
and host OS cannot access the capability's memory. This is achieved via
Arm's Realm Management Extension (RME), part of Armv9.3-A.

Integration path:
    Kata Containers + CoCo (Confidential Containers) + Arm CCA

Status: EXPERIMENTAL — No commercial Armv9+RME silicon is available yet.
    This implementation targets the Arm Fixed Virtual Platform (FVP)
    emulator and is behind a feature flag.

Prerequisites:
    - Armv9 hardware with RME (or FVP emulator)
    - Linux kernel with CONFIG_ARM64_RME=y
    - Kata Containers with CoCo runtime class
    - Realm Management Monitor (RMM) — tf-rmm or Islet
"""

import asyncio
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class RealmState(Enum):
    """Lifecycle states of a CCA Realm."""

    PENDING = "pending"          # Not yet created
    CREATING = "creating"        # VM being launched
    ATTESTING = "attesting"      # Realm attestation in progress
    RUNNING = "running"          # Capability running inside Realm
    STOPPING = "stopping"        # Graceful shutdown
    TERMINATED = "terminated"    # Realm destroyed
    FAILED = "failed"            # Creation or attestation failed


@dataclass
class RealmConfig:
    """Configuration for a CCA Realm container."""

    capability_id: str
    image: str
    vcpus: int = 1
    memory_mb: int = 256
    kata_runtime_class: str = "kata-cca"
    attestation_required: bool = True
    env: Dict[str, str] = None

    def __post_init__(self):
        if self.env is None:
            self.env = {}


@dataclass
class RealmInfo:
    """Runtime information about an active Realm."""

    realm_id: str
    capability_id: str
    state: RealmState
    attestation_token: Optional[Dict[str, Any]] = None
    container_id: Optional[str] = None
    zenoh_endpoint: Optional[str] = None


class RealmManager:
    """Manages CCA Realm lifecycle for capability containers.

    Launches capabilities inside Arm CCA Realms via Kata Containers
    + Confidential Containers (CoCo). Handles Realm creation,
    attestation, monitoring, and teardown.

    Usage:
        manager = RealmManager(device_id="device-001")

        realm = await manager.create_realm(RealmConfig(
            capability_id="crypto-signer",
            image="dc-cap-crypto:latest",
        ))

        # Wait for attestation to complete
        await manager.wait_ready(realm.realm_id)

        # Tear down
        await manager.destroy_realm(realm.realm_id)
    """

    def __init__(
        self,
        device_id: str,
        tenant: str = "default",
        kata_runtime: str = "io.containerd.kata-cca.v2",
    ):
        self._device_id = device_id
        self._tenant = tenant
        self._kata_runtime = kata_runtime
        self._realms: Dict[str, RealmInfo] = {}
        self._available = self._check_cca_available()

    def _check_cca_available(self) -> bool:
        """Check if Arm CCA / Realm support is available."""
        # Check 1: Architecture
        import platform
        if platform.machine().lower() not in ("aarch64", "arm64"):
            logger.info("CCA Realms: not Arm architecture, feature disabled")
            return False

        # Check 2: RME kernel support
        try:
            with open("/sys/devices/system/cpu/cpu0/regs/identification/id_aa64pfr0_el1") as f:
                pfr0 = int(f.read().strip(), 16)
                # RME is indicated by ID_AA64PFR0_EL1.RME field (bits [55:52])
                rme_field = (pfr0 >> 52) & 0xF
                if rme_field == 0:
                    logger.info("CCA Realms: RME not supported by this CPU")
                    return False
        except (FileNotFoundError, ValueError):
            # FVP or non-standard platform — check for Kata runtime instead
            pass

        # Check 3: Kata + CoCo runtime
        try:
            result = subprocess.run(
                ["ctr", "--runtime", self._kata_runtime, "info"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                logger.info("CCA Realms: Kata CCA runtime available")
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        logger.info("CCA Realms: runtime not available, feature disabled")
        return False

    @property
    def is_available(self) -> bool:
        """Whether CCA Realm support is available on this platform."""
        return self._available

    async def create_realm(self, config: RealmConfig) -> RealmInfo:
        """Create a CCA Realm and launch a capability inside it.

        Args:
            config: Realm configuration.

        Returns:
            RealmInfo with the initial state.

        Raises:
            RuntimeError: If CCA is not available.
        """
        if not self._available:
            raise RuntimeError(
                "CCA Realms not available. Requires Armv9+RME hardware "
                "and Kata Containers with CoCo. See Phase 3 documentation."
            )

        realm_id = f"realm-{config.capability_id}-{os.urandom(4).hex()}"

        info = RealmInfo(
            realm_id=realm_id,
            capability_id=config.capability_id,
            state=RealmState.CREATING,
        )
        self._realms[realm_id] = info

        try:
            # Launch container inside a CCA Realm via Kata
            container_id = await self._launch_kata_container(config, realm_id)
            info.container_id = container_id
            info.state = RealmState.ATTESTING

            # Perform attestation
            if config.attestation_required:
                token = await self._attest_realm(realm_id)
                info.attestation_token = token

            info.state = RealmState.RUNNING
            info.zenoh_endpoint = f"tcp/{realm_id}:7447"

            logger.info(
                "CCA Realm created: %s (capability=%s)",
                realm_id, config.capability_id,
            )
            return info

        except Exception as e:
            info.state = RealmState.FAILED
            logger.error("Failed to create Realm %s: %s", realm_id, e)
            raise

    async def _launch_kata_container(
        self, config: RealmConfig, realm_id: str,
    ) -> str:
        """Launch a container inside a Kata CCA VM.

        Uses containerd with the Kata CCA runtime class.
        """
        env_args = []
        all_env = {
            "ZENOH_ROUTER_ENDPOINT": f"tcp/zenoh-router:7447",
            "DEVICE_ID": self._device_id,
            "TENANT": self._tenant,
            "CAPABILITY_DIR": "/app/capability",
            **config.env,
        }
        for k, v in all_env.items():
            env_args.extend(["-e", f"{k}={v}"])

        cmd = [
            "ctr", "run",
            "--runtime", self._kata_runtime,
            "--memory-limit", str(config.memory_mb * 1024 * 1024),
            "--cpus", str(config.vcpus),
            *env_args,
            "--rm",
            config.image,
            realm_id,
        ]

        logger.info("Launching Kata CCA container: %s", " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Don't wait for completion — container runs in background
        # Return the realm_id as the container identifier
        return realm_id

    async def _attest_realm(self, realm_id: str) -> Dict[str, Any]:
        """Perform Realm attestation via CoCo Attestation Agent.

        The Attestation Agent runs inside the guest VM and communicates
        with the Trustee/KBS to verify the Realm's TCB.

        Returns:
            Attestation token/result.
        """
        # In production: the CoCo Attestation Agent inside the Kata VM
        # generates a CCA attestation token, sends it to Trustee,
        # and receives verified claims + secrets.
        #
        # For development/FVP: return a placeholder token.
        logger.info("Attesting Realm %s (would use CoCo Trustee in production)", realm_id)

        return {
            "realm_id": realm_id,
            "attested": True,
            "cca_token": {
                "realm_challenge": os.urandom(64).hex(),
                "realm_measurement": os.urandom(32).hex(),
                "platform_claims": {
                    "implementation_id": "arm-fvp-reference",
                    "security_lifecycle": "secured",
                },
            },
            "timestamp": __import__("time").time(),
        }

    async def wait_ready(self, realm_id: str, timeout: float = 120.0) -> bool:
        """Wait for a Realm to reach RUNNING state.

        Args:
            realm_id: Realm identifier.
            timeout: Maximum seconds to wait.

        Returns:
            True if ready, False if timed out.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            info = self._realms.get(realm_id)
            if info is None:
                return False
            if info.state == RealmState.RUNNING:
                return True
            if info.state == RealmState.FAILED:
                return False
            await asyncio.sleep(1.0)
        return False

    async def destroy_realm(self, realm_id: str) -> None:
        """Destroy a CCA Realm and clean up.

        Args:
            realm_id: Realm identifier.
        """
        info = self._realms.get(realm_id)
        if info is None:
            return

        info.state = RealmState.STOPPING

        if info.container_id:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ctr", "task", "kill", info.container_id,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.wait()
            except Exception as e:
                logger.warning("Error killing Realm container %s: %s", realm_id, e)

        info.state = RealmState.TERMINATED
        logger.info("CCA Realm destroyed: %s", realm_id)

    async def destroy_all(self) -> None:
        """Destroy all active Realms."""
        for realm_id in list(self._realms.keys()):
            await self.destroy_realm(realm_id)
        self._realms.clear()

    def get_realm_info(self, realm_id: str) -> Optional[RealmInfo]:
        """Get info about a Realm."""
        return self._realms.get(realm_id)

    def list_realms(self) -> List[RealmInfo]:
        """List all active Realms."""
        return list(self._realms.values())
