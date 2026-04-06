"""Unit tests for device_connect_container.security module.

Tests cover:
- AttestationTokenGenerator software-only token generation
- AttestationClaims defaults
- PolicyEngine with various rules
- MTE availability detection (non-Arm platforms)
- ImageVerifier tool availability check
"""

import json
import os
import platform
from unittest.mock import patch, MagicMock

import pytest

from device_connect_container.security.attestation import (
    AttestationTokenGenerator,
    AttestationClaims,
)
from device_connect_container.security.policy import (
    PolicyEngine,
    PolicyResult,
    RequireHardwareSecurity,
    RequireSecuredLifecycle,
    RequireSoftwareMeasurement,
    AllowInsecure,
)
from device_connect_container.security.mte import (
    is_mte_available,
    get_mte_status,
    MteMode,
)


# -- AttestationClaims --


class TestAttestationClaims:
    def test_defaults(self):
        claims = AttestationClaims()
        assert claims.security_lifecycle == 0x3000
        assert claims.profile == "PSA_IOT_PROFILE_1"
        assert len(claims.instance_id) == 16  # uuid bytes
        assert len(claims.boot_seed) == 32
        assert claims.nonce is None

    def test_custom_values(self):
        claims = AttestationClaims(
            security_lifecycle=0x0000,
            profile="NO_HARDWARE_SECURITY",
            nonce=b"challenge",
        )
        assert claims.security_lifecycle == 0x0000
        assert claims.profile == "NO_HARDWARE_SECURITY"
        assert claims.nonce == b"challenge"


# -- AttestationTokenGenerator --


class TestAttestationTokenGenerator:
    def test_software_token_generated(self):
        gen = AttestationTokenGenerator(
            device_id="test-device",
            device_type="camera",
            use_parsec=False,
        )
        token = gen.generate_token()

        assert "psa_attestation_token" in token
        assert "metadata" in token
        assert "signature" in token
        assert token["metadata"]["device_id"] == "test-device"
        assert token["metadata"]["hardware_backed"] is False

    def test_software_token_profile(self):
        gen = AttestationTokenGenerator(
            device_id="dev-1",
            use_parsec=False,
        )
        token = gen.generate_token()

        psa = token["psa_attestation_token"]
        assert psa["profile"] == "NO_HARDWARE_SECURITY"
        assert psa["security_lifecycle"] == 0x0000

    def test_software_components_measured(self):
        gen = AttestationTokenGenerator(
            device_id="dev-1",
            use_parsec=False,
        )
        token = gen.generate_token()

        sw = token["psa_attestation_token"]["sw_components"]
        types = [c["measurement_type"] for c in sw]
        assert "python_runtime" in types

    def test_container_image_hash_included(self):
        gen = AttestationTokenGenerator(
            device_id="dev-1",
            use_parsec=False,
        )
        token = gen.generate_token(container_image_hash="sha256:abc123")

        sw = token["psa_attestation_token"]["sw_components"]
        image_components = [c for c in sw if c["measurement_type"] == "container_image"]
        assert len(image_components) == 1
        assert image_components[0]["measurement_value"] == "sha256:abc123"

    def test_nonce_included_when_provided(self):
        gen = AttestationTokenGenerator(
            device_id="dev-1",
            use_parsec=False,
        )
        token = gen.generate_token(nonce=b"test-nonce")

        psa = token["psa_attestation_token"]
        assert psa["nonce"] is not None


# -- PolicyEngine --


class TestPolicyEngine:
    def _make_hw_token(self):
        return {
            "psa_attestation_token": {
                "profile": "PSA_IOT_PROFILE_1",
                "security_lifecycle": 0x3000,
                "sw_components": [],
            },
            "metadata": {"hardware_backed": True},
        }

    def _make_sw_token(self):
        return {
            "psa_attestation_token": {
                "profile": "NO_HARDWARE_SECURITY",
                "security_lifecycle": 0x0000,
                "sw_components": [],
            },
            "metadata": {"hardware_backed": False},
        }

    def test_no_rules_allows(self):
        engine = PolicyEngine()
        result = engine.evaluate(self._make_sw_token())
        assert result.allowed is True

    def test_allow_insecure_always_passes(self):
        engine = PolicyEngine()
        engine.add_rule(AllowInsecure())
        result = engine.evaluate(self._make_sw_token())
        assert result.allowed is True

    def test_require_hardware_rejects_software(self):
        engine = PolicyEngine()
        engine.add_rule(RequireHardwareSecurity())
        result = engine.evaluate(self._make_sw_token())
        assert result.allowed is False

    def test_require_hardware_accepts_hardware(self):
        engine = PolicyEngine()
        engine.add_rule(RequireHardwareSecurity())
        result = engine.evaluate(self._make_hw_token())
        assert result.allowed is True

    def test_require_secured_lifecycle_rejects_low(self):
        engine = PolicyEngine()
        engine.add_rule(RequireSecuredLifecycle())
        result = engine.evaluate(self._make_sw_token())
        assert result.allowed is False

    def test_require_secured_lifecycle_accepts_secured(self):
        engine = PolicyEngine()
        engine.add_rule(RequireSecuredLifecycle())
        result = engine.evaluate(self._make_hw_token())
        assert result.allowed is True

    def test_require_software_measurement_missing(self):
        engine = PolicyEngine()
        engine.add_rule(RequireSoftwareMeasurement({"container_image": "sha256:abc"}))
        result = engine.evaluate(self._make_hw_token())
        assert result.allowed is False

    def test_require_software_measurement_match(self):
        token = self._make_hw_token()
        token["psa_attestation_token"]["sw_components"] = [
            {"measurement_type": "container_image", "measurement_value": "sha256:abc"}
        ]
        engine = PolicyEngine()
        engine.add_rule(RequireSoftwareMeasurement({"container_image": "sha256:abc"}))
        result = engine.evaluate(token)
        assert result.allowed is True

    def test_production_default_rejects_software(self):
        engine = PolicyEngine.production_default()
        result = engine.evaluate(self._make_sw_token())
        assert result.allowed is False

    def test_development_default_accepts_anything(self):
        engine = PolicyEngine.development_default()
        result = engine.evaluate(self._make_sw_token())
        assert result.allowed is True

    def test_first_failure_stops_evaluation(self):
        engine = PolicyEngine()
        engine.add_rule(RequireHardwareSecurity())
        engine.add_rule(RequireSecuredLifecycle())
        result = engine.evaluate(self._make_sw_token())
        assert result.allowed is False
        assert "hardware" in result.reason.lower()


# -- MTE --


class TestMteDetection:
    def test_mte_not_available_on_non_arm(self):
        if platform.machine().lower() not in ("aarch64", "arm64"):
            assert is_mte_available() is False

    def test_get_mte_status_returns_dict(self):
        status = get_mte_status()
        assert isinstance(status, dict)
        assert "available" in status
        assert "enabled" in status
        assert "platform" in status

    def test_mte_mode_enum(self):
        assert MteMode.NONE == 0
        assert MteMode.SYNC != MteMode.ASYNC
