"""Tests for device_connect_server.security.credentials module."""

import json
import pytest

from device_connect_server.security.credentials import CredentialsLoader


# ── load_from_file ────────────────────────────────────────────────


class TestCredentialsLoaderFromFile:
    def test_load_json_format(self, tmp_path):
        creds_file = tmp_path / "device.creds.json"
        creds_file.write_text(json.dumps({
            "device_id": "cam-001",
            "tenant": "lab",
            "nats": {
                "urls": ["tls://nats:4222"],
                "jwt": "eyJ...",
                "nkey_seed": "SUAM...",
                "tls": {"ca_file": "/certs/ca.pem"},
            },
        }))
        result = CredentialsLoader.load_from_file(str(creds_file))
        assert result["device_id"] == "cam-001"
        assert result["tenant"] == "lab"
        assert result["jwt"] == "eyJ..."
        assert result["nkey_seed"] == "SUAM..."
        assert result["urls"] == ["tls://nats:4222"]
        assert result["tls"]["ca_file"] == "/certs/ca.pem"

    def test_load_json_single_url(self, tmp_path):
        creds_file = tmp_path / "device.creds.json"
        creds_file.write_text(json.dumps({
            "nats": {"url": "nats://localhost:4222"},
        }))
        result = CredentialsLoader.load_from_file(str(creds_file))
        assert result["urls"] == ["nats://localhost:4222"]

    def test_load_nats_creds_format(self, tmp_path):
        creds_file = tmp_path / "device.creds"
        creds_file.write_text(
            "-----BEGIN NATS USER JWT-----\n"
            "eyJhbGciOiJFZDI1NTE5\n"
            "------END NATS USER JWT------\n"
            "\n"
            "-----BEGIN USER NKEY SEED-----\n"
            "SUAM2XHKL7UQFZ7FQZJ\n"
            "------END USER NKEY SEED------\n"
        )
        result = CredentialsLoader.load_from_file(str(creds_file))
        assert result["jwt"] == "eyJhbGciOiJFZDI1NTE5"
        assert result["nkey_seed"] == "SUAM2XHKL7UQFZ7FQZJ"

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            CredentialsLoader.load_from_file("/nonexistent/path.creds")

    def test_malformed_json_raises(self, tmp_path):
        creds_file = tmp_path / "bad.creds.json"
        creds_file.write_text("{invalid json")
        with pytest.raises(ValueError, match="Invalid JSON"):
            CredentialsLoader.load_from_file(str(creds_file))

    def test_unknown_format_raises(self, tmp_path):
        creds_file = tmp_path / "weird.creds"
        creds_file.write_text("just plain text")
        with pytest.raises(ValueError, match="Unknown credentials format"):
            CredentialsLoader.load_from_file(str(creds_file))

    def test_mqtt_credentials(self, tmp_path):
        creds_file = tmp_path / "device.creds.json"
        creds_file.write_text(json.dumps({
            "mqtt": {"username": "device01", "password": "s3cret"},
        }))
        result = CredentialsLoader.load_from_file(str(creds_file))
        assert result["username"] == "device01"
        assert result["password"] == "s3cret"


# ── load_from_env ─────────────────────────────────────────────────


class TestCredentialsLoaderFromEnv:
    def test_jwt_from_env(self, monkeypatch):
        monkeypatch.setenv("NATS_JWT", "my-jwt")
        monkeypatch.setenv("NATS_NKEY_SEED", "my-seed")
        result = CredentialsLoader.load_from_env()
        assert result["jwt"] == "my-jwt"
        assert result["nkey_seed"] == "my-seed"

    def test_urls_from_env(self, monkeypatch):
        monkeypatch.setenv("NATS_URLS", "nats://a:4222, nats://b:4222")
        result = CredentialsLoader.load_from_env()
        assert result["urls"] == ["nats://a:4222", "nats://b:4222"]

    def test_single_url(self, monkeypatch):
        monkeypatch.setenv("NATS_URL", "nats://localhost:4222")
        result = CredentialsLoader.load_from_env()
        assert result["urls"] == ["nats://localhost:4222"]

    def test_tls_from_env(self, monkeypatch):
        monkeypatch.setenv("NATS_TLS_CA_FILE", "/ca.pem")
        monkeypatch.setenv("NATS_TLS_CERT_FILE", "/cert.pem")
        monkeypatch.setenv("NATS_TLS_KEY_FILE", "/key.pem")
        result = CredentialsLoader.load_from_env()
        assert result["tls"]["ca_file"] == "/ca.pem"
        assert result["tls"]["cert_file"] == "/cert.pem"

    def test_tls_messaging_vars_take_precedence(self, monkeypatch):
        monkeypatch.setenv("MESSAGING_TLS_CA_FILE", "/messaging-ca.pem")
        monkeypatch.setenv("MESSAGING_TLS_CERT_FILE", "/messaging-cert.pem")
        monkeypatch.setenv("MESSAGING_TLS_KEY_FILE", "/messaging-key.pem")
        monkeypatch.setenv("NATS_TLS_CA_FILE", "/nats-ca.pem")
        monkeypatch.setenv("NATS_TLS_CERT_FILE", "/nats-cert.pem")
        monkeypatch.setenv("NATS_TLS_KEY_FILE", "/nats-key.pem")
        result = CredentialsLoader.load_from_env()
        assert result["tls"]["ca_file"] == "/messaging-ca.pem"
        assert result["tls"]["cert_file"] == "/messaging-cert.pem"
        assert result["tls"]["key_file"] == "/messaging-key.pem"

    def test_tls_messaging_vars_alone(self, monkeypatch):
        monkeypatch.setenv("MESSAGING_TLS_CA_FILE", "/messaging-ca.pem")
        result = CredentialsLoader.load_from_env()
        assert result["tls"]["ca_file"] == "/messaging-ca.pem"

    def test_mqtt_from_env(self, monkeypatch):
        monkeypatch.setenv("MESSAGING_USERNAME", "user")
        monkeypatch.setenv("MESSAGING_PASSWORD", "pass")
        result = CredentialsLoader.load_from_env()
        assert result["username"] == "user"
        assert result["password"] == "pass"

    def test_device_id_and_tenant(self, monkeypatch):
        monkeypatch.setenv("DEVICE_ID", "cam-001")
        monkeypatch.setenv("TENANT", "lab")
        result = CredentialsLoader.load_from_env()
        assert result["device_id"] == "cam-001"
        assert result["tenant"] == "lab"

    def test_empty_env(self, monkeypatch):
        # Clear all relevant env vars
        for var in ("NATS_JWT", "NATS_NKEY_SEED", "NATS_URL", "NATS_URLS",
                    "NATS_CREDENTIALS_FILE", "NATS_TLS_CA_FILE",
                    "NATS_TLS_CERT_FILE", "NATS_TLS_KEY_FILE",
                    "MESSAGING_TLS_CA_FILE", "MESSAGING_TLS_CERT_FILE",
                    "MESSAGING_TLS_KEY_FILE",
                    "MESSAGING_USERNAME", "MESSAGING_PASSWORD",
                    "DEVICE_ID", "TENANT"):
            monkeypatch.delenv(var, raising=False)
        result = CredentialsLoader.load_from_env()
        assert result == {}


# ── Utility methods ───────────────────────────────────────────────


class TestCredentialsLoaderUtils:
    def test_get_urls_present(self):
        urls = CredentialsLoader.get_urls({"urls": ["tls://a:4222"]})
        assert urls == ["tls://a:4222"]

    def test_get_urls_default(self):
        urls = CredentialsLoader.get_urls({})
        assert urls == ["nats://localhost:4222"]

    def test_get_urls_custom_default(self):
        urls = CredentialsLoader.get_urls({}, default=["mqtt://broker:1883"])
        assert urls == ["mqtt://broker:1883"]

    def test_has_jwt_auth(self):
        assert CredentialsLoader.has_jwt_auth({"jwt": "x", "nkey_seed": "y"}) is True
        assert CredentialsLoader.has_jwt_auth({"jwt": "x"}) is False
        assert CredentialsLoader.has_jwt_auth({}) is False

    def test_has_password_auth(self):
        assert CredentialsLoader.has_password_auth({"username": "u", "password": "p"}) is True
        assert CredentialsLoader.has_password_auth({"username": "u"}) is False
        assert CredentialsLoader.has_password_auth({}) is False
