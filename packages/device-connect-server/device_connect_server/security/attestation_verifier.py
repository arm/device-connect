"""Server-side PSA attestation token verification via Veraison.

Verifies PSA attestation tokens submitted by devices during registration.
Integrates with the Veraison verification service (IETF RATS architecture)
to check token claims against provisioned reference values.

Verification flow:
    1. Device submits PSA token in registerDevice RPC
    2. Registry calls AttestationVerifier.verify()
    3. Verifier sends token to Veraison VTS (Verification Service)
    4. Veraison checks: signature, claims, reference values, freshness
    5. Returns EAR (Entity Attestation Result) with trust tier

Deployment:
    - Veraison runs as a set of containers (provisioning + verification services)
    - Reference values are provisioned via CoRIM (Concise Reference Integrity Manifest)
    - Can also run standalone with local policy (no Veraison) for development

Environment variables:
    VERAISON_URL          — Veraison verification service URL
    VERAISON_API_KEY      — API key for Veraison (optional)
    REQUIRE_ATTESTATION   — If "true", reject devices without valid attestation
"""

import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class AttestationVerifier:
    """Verify PSA attestation tokens using Veraison or local policy.

    In production, delegates to a Veraison verification service.
    In development, uses the local PolicyEngine from device-connect-container.

    Usage:
        verifier = AttestationVerifier()

        result = verifier.verify(token)
        if result["allowed"]:
            # Admit device
        else:
            # Reject: result["reason"]
    """

    def __init__(
        self,
        veraison_url: Optional[str] = None,
        veraison_api_key: Optional[str] = None,
        require_hardware: bool = False,
    ):
        """
        Args:
            veraison_url: URL of the Veraison verification service.
                If None, reads from VERAISON_URL env var.
                If still None, falls back to local policy verification.
            veraison_api_key: API key for Veraison.
            require_hardware: Reject software-only attestation tokens.
        """
        self._veraison_url = veraison_url or os.getenv("VERAISON_URL")
        self._veraison_api_key = veraison_api_key or os.getenv("VERAISON_API_KEY")
        self._require_hardware = require_hardware
        self._veraison_available = self._veraison_url is not None

        if self._veraison_available:
            logger.info("Attestation verifier: Veraison at %s", self._veraison_url)
        else:
            logger.info("Attestation verifier: local policy mode (no Veraison)")

    def verify(self, token: Dict[str, Any]) -> Dict[str, Any]:
        """Verify a PSA attestation token.

        Args:
            token: PSA attestation token dict (from AttestationTokenGenerator).

        Returns:
            Dict with:
                - "allowed": bool
                - "reason": str (explanation)
                - "trust_tier": str (veraison EAR tier, if available)
                - "details": dict (additional verification info)
        """
        if not token:
            return {
                "allowed": False,
                "reason": "No attestation token provided",
                "trust_tier": "none",
                "details": {},
            }

        if self._veraison_available:
            return self._verify_via_veraison(token)
        else:
            return self._verify_local(token)

    def _verify_via_veraison(self, token: Dict[str, Any]) -> Dict[str, Any]:
        """Verify using the Veraison verification service.

        Sends the PSA token to Veraison's ChallengeResponse API and
        interprets the EAR (Entity Attestation Result).
        """
        try:
            import urllib.request
            import urllib.error

            # Veraison ChallengeResponse API endpoint
            url = f"{self._veraison_url}/challenge-response/v1/session"

            # Build verification request
            psa_token = token.get("psa_attestation_token", {})
            nonce = psa_token.get("nonce")

            request_body = json.dumps({
                "type": "application/psa-attestation-token",
                "token": token,
                "nonce": nonce,
            }).encode()

            headers = {
                "Content-Type": "application/json",
                "Accept": "application/ear+jwt",
            }
            if self._veraison_api_key:
                headers["Authorization"] = f"Bearer {self._veraison_api_key}"

            req = urllib.request.Request(
                url, data=request_body, headers=headers, method="POST",
            )

            with urllib.request.urlopen(req, timeout=10) as resp:
                ear_result = json.loads(resp.read().decode())

            # Parse EAR trust tier
            # EAR trust tiers: "affirming", "warning", "contraindicated", "none"
            trust_tier = ear_result.get("ear.trust-tier", "none")
            allowed = trust_tier in ("affirming", "warning")

            return {
                "allowed": allowed,
                "reason": f"Veraison trust tier: {trust_tier}",
                "trust_tier": trust_tier,
                "details": ear_result,
            }

        except urllib.error.URLError as e:
            logger.error("Veraison verification request failed: %s", e)
            # Fall back to local verification
            logger.info("Falling back to local policy verification")
            return self._verify_local(token)

        except Exception as e:
            logger.error("Veraison verification error: %s", e)
            return {
                "allowed": False,
                "reason": f"Verification error: {e}",
                "trust_tier": "none",
                "details": {},
            }

    def _verify_local(self, token: Dict[str, Any]) -> Dict[str, Any]:
        """Verify using local policy engine (no Veraison).

        Falls back to the PolicyEngine from device-connect-container
        if available, otherwise performs basic field validation.
        """
        try:
            from device_connect_container.security.policy import PolicyEngine

            if self._require_hardware:
                engine = PolicyEngine.production_default()
            else:
                engine = PolicyEngine.development_default()

            result = engine.evaluate(token)
            return {
                "allowed": result.allowed,
                "reason": result.reason or "local_policy_passed",
                "trust_tier": "affirming" if result.allowed else "contraindicated",
                "details": result.details or {},
            }

        except ImportError:
            # device-connect-container not installed — basic validation
            return self._verify_basic(token)

    def _verify_basic(self, token: Dict[str, Any]) -> Dict[str, Any]:
        """Minimal validation without device-connect-container package."""
        metadata = token.get("metadata", {})
        psa = token.get("psa_attestation_token", {})

        hardware_backed = metadata.get("hardware_backed", False)
        profile = psa.get("profile", "")
        has_signature = "signature" in token

        if self._require_hardware and not hardware_backed:
            return {
                "allowed": False,
                "reason": "Hardware-backed attestation required",
                "trust_tier": "contraindicated",
                "details": {"profile": profile},
            }

        if not has_signature:
            return {
                "allowed": False,
                "reason": "Token missing signature",
                "trust_tier": "none",
                "details": {},
            }

        # Software-only token with signature — allow in dev mode
        return {
            "allowed": True,
            "reason": "basic_validation_passed",
            "trust_tier": "warning" if not hardware_backed else "affirming",
            "details": {"profile": profile, "hardware_backed": hardware_backed},
        }
