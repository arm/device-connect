"""PSA Attestation token generation for Device Connect devices.

Generates Platform Security Architecture (PSA) attestation tokens that
prove the integrity of the device hardware, firmware, and software state.
These tokens are verified by the server-side Veraison verifier before
the device is allowed to register.

Attestation flow:
    1. Device boots → generates PSA token via Parsec (or software fallback)
    2. Token included in device registration payload
    3. Server verifies token against reference values via Veraison
    4. If valid, device is admitted to the network

On hardware without PSA support (e.g., non-Arm platforms), a software-only
token with NO_HARDWARE_SECURITY claim is generated. Server policy decides
whether to accept or reject such tokens.

Requires: cryptography>=42.0 for token signing
"""

import base64
import hashlib
import json
import logging
import os
import platform
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class AttestationClaims:
    """PSA attestation token claims (subset of PSA Profile 2).

    See: draft-tschofenig-rats-psa-token-10
    """

    # Device identity
    instance_id: bytes = field(default_factory=lambda: uuid.uuid4().bytes)
    implementation_id: bytes = field(default_factory=lambda: b"\x00" * 32)

    # Security lifecycle state
    # 0x0000 = unknown, 0x1000 = assembly, 0x2000 = PSA_RoT_provisioning,
    # 0x3000 = secured, 0x4000 = non-PSA_RoT_debug, 0x5000 = recoverable_PSA_RoT_debug,
    # 0x6000 = decommissioned
    security_lifecycle: int = 0x3000  # secured

    # Boot seed (entropy from boot process)
    boot_seed: bytes = field(default_factory=lambda: os.urandom(32))

    # Software components (measurement of running software)
    sw_components: List[Dict[str, Any]] = field(default_factory=list)

    # Hardware version
    hardware_version: str = ""

    # Profile: PSA_IOT_PROFILE_1 or NO_HARDWARE_SECURITY
    profile: str = "PSA_IOT_PROFILE_1"

    # Nonce (challenge from verifier)
    nonce: Optional[bytes] = None


class AttestationTokenGenerator:
    """Generate PSA attestation tokens for device registration.

    Attempts to use Parsec for hardware-backed attestation. Falls back
    to software-only tokens on unsupported platforms.
    """

    def __init__(
        self,
        device_id: str,
        device_type: str = "unknown",
        use_parsec: bool = True,
    ):
        self._device_id = device_id
        self._device_type = device_type
        self._use_parsec = use_parsec
        self._parsec_client = None
        self._hardware_available = False

        self._detect_hardware()

    def _detect_hardware(self) -> None:
        """Detect available hardware security features."""
        arch = platform.machine().lower()
        self._is_arm = arch in ("aarch64", "arm64", "armv8l", "armv9l")

        if self._use_parsec and self._is_arm:
            try:
                self._parsec_client = self._connect_parsec()
                self._hardware_available = True
                logger.info("Parsec hardware security available")
            except Exception as e:
                logger.debug("Parsec not available: %s", e)
                self._hardware_available = False

    def _connect_parsec(self) -> Any:
        """Connect to Parsec security daemon.

        Returns:
            Parsec client instance.

        Raises:
            RuntimeError: If Parsec is not available.
        """
        # Parsec connects via Unix domain socket at /run/parsec/parsec.sock
        parsec_socket = os.getenv("PARSEC_SOCKET", "/run/parsec/parsec.sock")
        if not os.path.exists(parsec_socket):
            raise RuntimeError(f"Parsec socket not found: {parsec_socket}")

        # In production, use parsec-tool or parsec Python bindings
        # For now, return a placeholder
        logger.debug("Would connect to Parsec at %s", parsec_socket)
        raise RuntimeError("Parsec Python bindings not yet integrated")

    def generate_token(
        self,
        nonce: Optional[bytes] = None,
        container_image_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate a PSA attestation token.

        Args:
            nonce: Challenge nonce from verifier (for freshness).
            container_image_hash: SHA-256 hash of the OCI image.

        Returns:
            Attestation token as a dict (CBOR/COSE in production,
            JSON for development/testing).
        """
        # Build software component measurements
        sw_components = self._measure_software(container_image_hash)

        if self._hardware_available:
            claims = self._generate_hardware_claims(nonce, sw_components)
        else:
            claims = self._generate_software_claims(nonce, sw_components)

        # Build token
        token = {
            "psa_attestation_token": {
                "instance_id": base64.b64encode(claims.instance_id).decode(),
                "implementation_id": base64.b64encode(claims.implementation_id).decode(),
                "security_lifecycle": claims.security_lifecycle,
                "boot_seed": base64.b64encode(claims.boot_seed).decode(),
                "hardware_version": claims.hardware_version,
                "profile": claims.profile,
                "sw_components": claims.sw_components,
                "nonce": base64.b64encode(claims.nonce).decode() if claims.nonce else None,
            },
            "metadata": {
                "device_id": self._device_id,
                "device_type": self._device_type,
                "timestamp": time.time(),
                "hardware_backed": self._hardware_available,
                "platform": platform.machine(),
            },
        }

        # Sign the token
        token["signature"] = self._sign_token(token)

        logger.info(
            "Generated %s attestation token for %s",
            "hardware-backed" if self._hardware_available else "software-only",
            self._device_id,
        )
        return token

    def _generate_hardware_claims(
        self,
        nonce: Optional[bytes],
        sw_components: List[Dict[str, Any]],
    ) -> AttestationClaims:
        """Generate claims using hardware security (Parsec/PSA)."""
        # In production: Parsec calls PSA Crypto API to generate
        # hardware-attested claims with TPM/PSA-backed keys
        return AttestationClaims(
            instance_id=self._get_hardware_instance_id(),
            implementation_id=self._get_implementation_id(),
            security_lifecycle=0x3000,  # secured
            sw_components=sw_components,
            hardware_version=self._get_hardware_version(),
            profile="PSA_IOT_PROFILE_1",
            nonce=nonce,
        )

    def _generate_software_claims(
        self,
        nonce: Optional[bytes],
        sw_components: List[Dict[str, Any]],
    ) -> AttestationClaims:
        """Generate software-only claims (no hardware security)."""
        # Generate a deterministic instance ID from device_id
        instance_id = hashlib.sha256(self._device_id.encode()).digest()

        return AttestationClaims(
            instance_id=instance_id,
            implementation_id=b"\x00" * 32,
            security_lifecycle=0x0000,  # unknown (no hardware guarantee)
            sw_components=sw_components,
            hardware_version="",
            profile="NO_HARDWARE_SECURITY",
            nonce=nonce,
        )

    def _measure_software(
        self, container_image_hash: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Measure running software components.

        Returns list of software component claims with measurement hashes.
        """
        components = []

        # Measure Python runtime
        import sys
        components.append({
            "measurement_type": "python_runtime",
            "measurement_value": hashlib.sha256(
                sys.version.encode()
            ).hexdigest(),
            "version": sys.version.split()[0],
            "signer_id": "python.org",
        })

        # Measure device-connect-edge package
        try:
            import device_connect_edge
            version = getattr(device_connect_edge, "__version__", "unknown")
            components.append({
                "measurement_type": "device_connect_edge",
                "measurement_value": hashlib.sha256(
                    version.encode()
                ).hexdigest(),
                "version": version,
                "signer_id": "arm.com",
            })
        except ImportError:
            pass

        # Measure container image if hash provided
        if container_image_hash:
            components.append({
                "measurement_type": "container_image",
                "measurement_value": container_image_hash,
                "signer_id": "oci",
            })

        return components

    def _get_hardware_instance_id(self) -> bytes:
        """Get hardware instance ID (from Parsec/TPM)."""
        if self._parsec_client:
            # parsec_client.psa_export_public_key("instance_id")
            pass
        return os.urandom(32)

    def _get_implementation_id(self) -> bytes:
        """Get implementation ID (SoC/board identifier)."""
        # Read from /sys/firmware/devicetree/base/model on Arm Linux
        try:
            with open("/sys/firmware/devicetree/base/model", "rb") as f:
                model = f.read().strip(b"\x00")
                return hashlib.sha256(model).digest()
        except FileNotFoundError:
            return hashlib.sha256(platform.machine().encode()).digest()

    def _get_hardware_version(self) -> str:
        """Get hardware version string."""
        try:
            with open("/sys/firmware/devicetree/base/model", "r") as f:
                return f.read().strip("\x00").strip()
        except FileNotFoundError:
            return platform.machine()

    def _sign_token(self, token: Dict[str, Any]) -> str:
        """Sign the attestation token.

        In production: uses Parsec/PSA Crypto with hardware-backed key.
        In development: uses HMAC-SHA256 with a software key.

        Returns:
            Base64-encoded signature.
        """
        import hmac

        # Software signing key (in production, this would be a PSA key)
        signing_key = os.getenv(
            "ATTESTATION_SIGNING_KEY",
            self._device_id,  # Fallback for development
        ).encode()

        # Canonical JSON for deterministic signing
        payload = json.dumps(
            token.get("psa_attestation_token", {}),
            sort_keys=True,
        ).encode()

        signature = hmac.new(signing_key, payload, hashlib.sha256).digest()
        return base64.b64encode(signature).decode()
