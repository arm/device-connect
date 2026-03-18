"""Tests for device_connect_server.security.commissioning module."""

import json
import time
import pytest

from device_connect_server.security.commissioning import (
    CommissioningPIN,
    CommissioningMode,
    generate_factory_pin,
    format_pin,
    parse_pin,
)


# ── CommissioningPIN dataclass ────────────────────────────────────


class TestCommissioningPIN:
    def test_fields(self):
        pin = CommissioningPIN(
            pin="12345678",
            pin_hash="$2b$12$...",
            device_id="cam-001",
            device_type="camera",
            created_at="2024-01-01T00:00:00Z",
        )
        assert pin.pin == "12345678"
        assert pin.commissioned is False
        assert pin.commissioned_at is None

    def test_commissioned(self):
        pin = CommissioningPIN(
            pin="12345678",
            pin_hash="hash",
            device_id="cam-001",
            device_type="camera",
            created_at="2024-01-01",
            commissioned=True,
            commissioned_at="2024-01-02",
        )
        assert pin.commissioned is True
        assert pin.commissioned_at == "2024-01-02"


# ── PIN utilities ─────────────────────────────────────────────────


class TestPINGeneration:
    def test_generate_pin_length(self):
        pin = generate_factory_pin()
        assert len(pin) == 8
        assert pin.isdigit()

    def test_generate_pin_unique(self):
        pins = {generate_factory_pin() for _ in range(20)}
        assert len(pins) > 1  # statistically near-certain

    def test_generate_pin_range(self):
        pin = generate_factory_pin()
        assert 10000000 <= int(pin) <= 99999999


class TestFormatPin:
    def test_format(self):
        assert format_pin("12345678") == "1234-5678"

    def test_invalid_length(self):
        with pytest.raises(ValueError, match="8 digits"):
            format_pin("123")


class TestParsePin:
    def test_parse_formatted(self):
        assert parse_pin("1234-5678") == "12345678"

    def test_parse_with_spaces(self):
        assert parse_pin("1234 5678") == "12345678"


# ── CommissioningMode ─────────────────────────────────────────────


class TestCommissioningMode:
    """Tests for the commissioning mode (requires bcrypt)."""

    @pytest.fixture
    def mode(self):
        try:
            return CommissioningMode(
                device_id="cam-001",
                device_type="camera",
                factory_pin="12345678",
                capabilities=["capture_image"],
            )
        except ImportError:
            pytest.skip("bcrypt not installed")

    def test_init(self, mode):
        assert mode.device_id == "cam-001"
        assert mode.device_type == "camera"
        assert mode.commissioned is False
        assert mode.commission_attempts == 0

    def test_valid_pin(self, mode):
        valid, error = mode.validate_pin("12345678")
        assert valid is True
        assert error is None

    def test_invalid_pin(self, mode):
        valid, error = mode.validate_pin("00000000")
        assert valid is False
        assert "Invalid PIN" in error

    def test_already_commissioned(self, mode):
        mode.validate_pin("12345678")
        mode.commissioned = True
        valid, error = mode.validate_pin("12345678")
        assert valid is False
        assert "already commissioned" in error

    def test_rate_limiting(self, mode):
        mode.max_attempts = 2
        mode.validate_pin("wrong1__")
        mode.validate_pin("wrong2__")
        valid, error = mode.validate_pin("12345678")
        assert valid is False
        assert "Too many attempts" in error

    def test_rate_limit_resets(self, mode):
        mode.max_attempts = 1
        mode.lockout_duration = 0  # instant unlock
        mode.validate_pin("wrong1__")
        # Force elapsed time > lockout
        mode.last_attempt_time = time.time() - 1
        valid, _ = mode.validate_pin("12345678")
        assert valid is True

    def test_save_credentials(self, mode, tmp_path):
        creds = {"nats": {"urls": ["tls://nats:4222"], "jwt": "tok"}}
        path = str(tmp_path / "device.creds")
        mode.save_credentials(creds, path=path)

        saved = json.loads(open(path).read())
        assert saved["nats"]["jwt"] == "tok"

    def test_bcrypt_not_available(self, monkeypatch):
        import device_connect_server.security.commissioning as mod
        monkeypatch.setattr(mod, "_BCRYPT_AVAILABLE", False)
        with pytest.raises(ImportError, match="bcrypt"):
            CommissioningMode(
                device_id="x",
                device_type="y",
                factory_pin="12345678",
                capabilities=[],
            )
