"""Attestation policy engine for Device Connect.

Evaluates PSA attestation tokens against configurable policies to
decide whether a device/capability is trusted enough to join the network.

Policies are composable rules:
- RequireHardwareSecurity: Reject software-only attestation tokens
- RequireSecuredLifecycle: Require PSA security_lifecycle >= 0x3000
- RequireSoftwareMeasurement: Require specific SW component hashes
- AllowInsecure: Accept any token (development only)

Usage:
    engine = PolicyEngine()
    engine.add_rule(RequireHardwareSecurity())
    engine.add_rule(RequireSecuredLifecycle())

    result = engine.evaluate(attestation_token)
    if result.allowed:
        # Admit device
    else:
        # Reject with result.reason
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class PolicyResult:
    """Result of a policy evaluation."""

    allowed: bool
    reason: str = ""
    details: Dict[str, Any] = None

    def __post_init__(self):
        if self.details is None:
            self.details = {}


class PolicyRule(ABC):
    """Abstract attestation policy rule."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def evaluate(self, token: Dict[str, Any]) -> PolicyResult:
        """Evaluate this rule against an attestation token.

        Args:
            token: PSA attestation token dict.

        Returns:
            PolicyResult indicating pass/fail.
        """
        ...


class RequireHardwareSecurity(PolicyRule):
    """Reject tokens without hardware-backed attestation."""

    @property
    def name(self) -> str:
        return "require_hardware_security"

    def evaluate(self, token: Dict[str, Any]) -> PolicyResult:
        metadata = token.get("metadata", {})
        hardware_backed = metadata.get("hardware_backed", False)

        psa = token.get("psa_attestation_token", {})
        profile = psa.get("profile", "")

        if not hardware_backed or profile == "NO_HARDWARE_SECURITY":
            return PolicyResult(
                allowed=False,
                reason="Hardware-backed attestation required but token is software-only",
                details={"profile": profile, "hardware_backed": hardware_backed},
            )
        return PolicyResult(allowed=True)


class RequireSecuredLifecycle(PolicyRule):
    """Require PSA security_lifecycle to be at least 'secured' (0x3000)."""

    @property
    def name(self) -> str:
        return "require_secured_lifecycle"

    def evaluate(self, token: Dict[str, Any]) -> PolicyResult:
        psa = token.get("psa_attestation_token", {})
        lifecycle = psa.get("security_lifecycle", 0)

        if lifecycle < 0x3000:
            return PolicyResult(
                allowed=False,
                reason=f"Security lifecycle 0x{lifecycle:04X} below minimum 0x3000 (secured)",
                details={"security_lifecycle": lifecycle},
            )
        return PolicyResult(allowed=True)


class RequireSoftwareMeasurement(PolicyRule):
    """Require specific software component measurements."""

    def __init__(self, required_components: Optional[Dict[str, str]] = None):
        """
        Args:
            required_components: Dict mapping measurement_type to expected
                measurement_value hash.
        """
        self._required = required_components or {}

    @property
    def name(self) -> str:
        return "require_software_measurement"

    def evaluate(self, token: Dict[str, Any]) -> PolicyResult:
        psa = token.get("psa_attestation_token", {})
        sw_components = psa.get("sw_components", [])

        # Build lookup
        measured = {
            c["measurement_type"]: c["measurement_value"]
            for c in sw_components
            if "measurement_type" in c
        }

        for req_type, req_hash in self._required.items():
            actual = measured.get(req_type)
            if actual is None:
                return PolicyResult(
                    allowed=False,
                    reason=f"Required software component '{req_type}' not measured",
                )
            if actual != req_hash:
                return PolicyResult(
                    allowed=False,
                    reason=f"Software component '{req_type}' measurement mismatch",
                    details={"expected": req_hash, "actual": actual},
                )

        return PolicyResult(allowed=True)


class AllowInsecure(PolicyRule):
    """Accept any attestation token (development only)."""

    @property
    def name(self) -> str:
        return "allow_insecure"

    def evaluate(self, token: Dict[str, Any]) -> PolicyResult:
        logger.warning("AllowInsecure policy: accepting token without verification")
        return PolicyResult(allowed=True)


class PolicyEngine:
    """Evaluate attestation tokens against a set of policy rules.

    Rules are evaluated in order. The first failure stops evaluation.
    """

    def __init__(self) -> None:
        self._rules: List[PolicyRule] = []

    def add_rule(self, rule: PolicyRule) -> None:
        """Add a policy rule."""
        self._rules.append(rule)

    def evaluate(self, token: Dict[str, Any]) -> PolicyResult:
        """Evaluate all rules against an attestation token.

        Args:
            token: PSA attestation token dict.

        Returns:
            PolicyResult. If all rules pass, allowed=True.
            If any rule fails, returns the first failure.
        """
        if not self._rules:
            logger.warning("No attestation policy rules configured — allowing by default")
            return PolicyResult(allowed=True, reason="no_policy")

        for rule in self._rules:
            result = rule.evaluate(token)
            if not result.allowed:
                logger.info(
                    "Attestation policy '%s' denied: %s",
                    rule.name, result.reason,
                )
                return result

        return PolicyResult(allowed=True)

    @classmethod
    def production_default(cls) -> "PolicyEngine":
        """Create a policy engine with production-default rules."""
        engine = cls()
        engine.add_rule(RequireHardwareSecurity())
        engine.add_rule(RequireSecuredLifecycle())
        return engine

    @classmethod
    def development_default(cls) -> "PolicyEngine":
        """Create a permissive policy engine for development."""
        engine = cls()
        engine.add_rule(AllowInsecure())
        return engine
