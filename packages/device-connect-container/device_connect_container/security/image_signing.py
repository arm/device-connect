"""OCI container image signing and verification.

Wraps Cosign (Sigstore) and Notation (Notary Project) for signing
Device Connect capability container images. Supports:

1. Keyless signing (Fulcio + Rekor) for CI/CD pipelines
2. Key-pair signing for offline/edge environments
3. Hardware key signing via PKCS#11 (YubiKey, HSM)

Image signatures ensure that:
- Container images haven't been tampered with
- Images come from a trusted build pipeline
- The ContainerCapabilityLoader can verify image integrity before launch

Usage:
    signer = ImageSigner()

    # Sign in CI/CD (keyless)
    signer.sign_keyless("ghcr.io/arm/dc-cap-vision:1.0")

    # Sign with key pair
    signer.sign_with_key("dc-cap-vision:latest", key_path="cosign.key")

    # Verify before launch
    verifier = ImageVerifier()
    if verifier.verify("dc-cap-vision:latest"):
        # Safe to launch
        ...
"""

import logging
import os
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


class ImageSigner:
    """Sign OCI container images using Cosign or Notation."""

    def __init__(
        self,
        tool: str = "cosign",
        cosign_path: str = "cosign",
        notation_path: str = "notation",
    ):
        self._tool = tool
        self._cosign_path = cosign_path
        self._notation_path = notation_path

    def sign_keyless(self, image: str, recursive: bool = True) -> bool:
        """Sign an image using keyless signing (Sigstore Fulcio + Rekor).

        Requires OIDC identity (CI/CD service account or interactive browser).
        The signature is recorded in the Rekor transparency log.

        Args:
            image: Full image reference (e.g., "ghcr.io/arm/dc-cap-vision:1.0").
            recursive: Sign all architectures in a multi-arch manifest.

        Returns:
            True if signed successfully.
        """
        cmd = [self._cosign_path, "sign"]
        if recursive:
            cmd.append("--recursive")
        cmd.append(image)

        env = dict(os.environ)
        env["COSIGN_EXPERIMENTAL"] = "1"

        logger.info("Signing image (keyless): %s", image)
        result = subprocess.run(cmd, env=env, capture_output=True)

        if result.returncode != 0:
            logger.error("Keyless signing failed: %s", result.stderr.decode())
            return False

        logger.info("Image signed (keyless): %s", image)
        return True

    def sign_with_key(self, image: str, key_path: str, recursive: bool = True) -> bool:
        """Sign an image using a key pair.

        Args:
            image: Full image reference.
            key_path: Path to private key (cosign.key or PEM).
            recursive: Sign all architectures in a multi-arch manifest.

        Returns:
            True if signed successfully.
        """
        cmd = [self._cosign_path, "sign", "--key", key_path]
        if recursive:
            cmd.append("--recursive")
        cmd.append(image)

        logger.info("Signing image (key): %s", image)
        result = subprocess.run(cmd, capture_output=True)

        if result.returncode != 0:
            logger.error("Key signing failed: %s", result.stderr.decode())
            return False

        logger.info("Image signed (key): %s", image)
        return True

    def sign_with_hardware_key(self, image: str, slot: str = "", recursive: bool = True) -> bool:
        """Sign using a hardware security key (YubiKey, PKCS#11 token).

        Requires Cosign built with pivkey or pkcs11key tags.

        Args:
            image: Full image reference.
            slot: PKCS#11 slot URI (optional for YubiKey PIV).
            recursive: Sign all architectures.

        Returns:
            True if signed successfully.
        """
        cmd = [self._cosign_path, "sign", "--sk"]
        if slot:
            cmd.extend(["--slot", slot])
        if recursive:
            cmd.append("--recursive")
        cmd.append(image)

        logger.info("Signing image (hardware key): %s", image)
        result = subprocess.run(cmd, capture_output=True)

        if result.returncode != 0:
            logger.error("Hardware key signing failed: %s", result.stderr.decode())
            return False

        logger.info("Image signed (hardware key): %s", image)
        return True

    def sign_with_notation(self, image: str, key_name: str = "default") -> bool:
        """Sign using Notation (Notary Project).

        Args:
            image: Full image reference.
            key_name: Key name configured in Notation.

        Returns:
            True if signed successfully.
        """
        cmd = [self._notation_path, "sign", "--key", key_name, image]

        logger.info("Signing image (notation): %s", image)
        result = subprocess.run(cmd, capture_output=True)

        if result.returncode != 0:
            logger.error("Notation signing failed: %s", result.stderr.decode())
            return False

        logger.info("Image signed (notation): %s", image)
        return True


class ImageVerifier:
    """Verify OCI container image signatures before launch.

    Used by ContainerCapabilityLoader to ensure image integrity
    before running capability sidecars.
    """

    def __init__(
        self,
        tool: str = "cosign",
        cosign_path: str = "cosign",
        notation_path: str = "notation",
    ):
        self._tool = tool
        self._cosign_path = cosign_path
        self._notation_path = notation_path

    def verify(
        self,
        image: str,
        public_key: Optional[str] = None,
        certificate: Optional[str] = None,
        certificate_identity: Optional[str] = None,
        certificate_oidc_issuer: Optional[str] = None,
    ) -> bool:
        """Verify an image signature.

        For keyless signatures, provide certificate_identity and
        certificate_oidc_issuer. For key-based, provide public_key.

        Args:
            image: Full image reference.
            public_key: Path to public key (for key-based signatures).
            certificate: Path to certificate (for cert-based).
            certificate_identity: Expected identity in keyless certificate.
            certificate_oidc_issuer: Expected OIDC issuer.

        Returns:
            True if signature is valid.
        """
        if self._tool == "cosign":
            return self._verify_cosign(
                image, public_key, certificate,
                certificate_identity, certificate_oidc_issuer,
            )
        elif self._tool == "notation":
            return self._verify_notation(image)
        else:
            logger.error("Unknown verification tool: %s", self._tool)
            return False

    def _verify_cosign(
        self,
        image: str,
        public_key: Optional[str],
        certificate: Optional[str],
        certificate_identity: Optional[str],
        certificate_oidc_issuer: Optional[str],
    ) -> bool:
        cmd = [self._cosign_path, "verify"]

        if public_key:
            cmd.extend(["--key", public_key])
        elif certificate:
            cmd.extend(["--certificate", certificate])
        elif certificate_identity and certificate_oidc_issuer:
            cmd.extend([
                "--certificate-identity", certificate_identity,
                "--certificate-oidc-issuer", certificate_oidc_issuer,
            ])

        cmd.append(image)
        result = subprocess.run(cmd, capture_output=True)

        if result.returncode != 0:
            logger.warning("Image verification failed for %s: %s", image, result.stderr.decode())
            return False

        logger.info("Image verified: %s", image)
        return True

    def _verify_notation(self, image: str) -> bool:
        cmd = [self._notation_path, "verify", image]
        result = subprocess.run(cmd, capture_output=True)

        if result.returncode != 0:
            logger.warning("Notation verification failed for %s: %s", image, result.stderr.decode())
            return False

        logger.info("Image verified (notation): %s", image)
        return True

    def is_tool_available(self) -> bool:
        """Check if the signing/verification tool is installed."""
        tool_path = self._cosign_path if self._tool == "cosign" else self._notation_path
        try:
            result = subprocess.run([tool_path, "version"], capture_output=True, timeout=5)
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
