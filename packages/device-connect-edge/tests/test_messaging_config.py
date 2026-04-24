# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for device_connect_edge.messaging.config module."""

import os
from unittest.mock import patch

from device_connect_edge.messaging.config import MessagingConfig


class TestMessagingConfigDefaults:
    def test_default_backend(self):
        with patch.dict(os.environ, {}, clear=True):
            config = MessagingConfig()
            assert config.backend == "zenoh"

    def test_default_servers(self):
        with patch.dict(os.environ, {}, clear=True):
            config = MessagingConfig()
            assert config.servers == ["tcp/localhost:7447"]

    def test_default_credentials_none(self):
        with patch.dict(os.environ, {}, clear=True):
            config = MessagingConfig()
            assert config.credentials is None

    def test_default_tls_none(self):
        with patch.dict(os.environ, {}, clear=True):
            config = MessagingConfig()
            assert config.tls_config is None


class TestMessagingConfigDirect:
    def test_direct_backend(self):
        config = MessagingConfig(backend="mqtt")
        assert config.backend == "mqtt"

    def test_direct_servers(self):
        config = MessagingConfig(servers=["nats://host1:4222", "nats://host2:4222"])
        assert config.servers == ["nats://host1:4222", "nats://host2:4222"]

    def test_direct_credentials(self):
        creds = {"jwt": "my-jwt", "nkey_seed": "my-seed"}
        config = MessagingConfig(credentials=creds)
        assert config.credentials == creds

    def test_direct_tls(self):
        tls = {"ca_file": "/path/to/ca.pem"}
        config = MessagingConfig(tls_config=tls)
        assert config.tls_config == tls


class TestMessagingConfigEnv:
    def test_backend_from_env(self):
        with patch.dict(os.environ, {"MESSAGING_BACKEND": "MQTT"}, clear=True):
            config = MessagingConfig()
            assert config.backend == "mqtt"

    def test_servers_from_messaging_urls(self):
        with patch.dict(os.environ, {"MESSAGING_URLS": "nats://a:4222,nats://b:4222"}, clear=True):
            config = MessagingConfig()
            assert config.servers == ["nats://a:4222", "nats://b:4222"]

    def test_servers_from_nats_urls(self):
        with patch.dict(os.environ, {"NATS_URLS": "nats://c:4222"}, clear=True):
            config = MessagingConfig()
            assert config.servers == ["nats://c:4222"]

    def test_servers_from_nats_url(self):
        with patch.dict(os.environ, {"NATS_URL": "nats://d:4222"}, clear=True):
            config = MessagingConfig()
            assert config.servers == ["nats://d:4222"]

    def test_servers_priority_messaging_urls_over_nats(self):
        with patch.dict(os.environ, {
            "MESSAGING_URLS": "nats://a:4222",
            "NATS_URL": "nats://b:4222",
        }, clear=True):
            config = MessagingConfig()
            assert config.servers == ["nats://a:4222"]

    def test_credentials_from_jwt_env(self):
        with patch.dict(os.environ, {
            "NATS_JWT": "test-jwt",
            "NATS_NKEY_SEED": "test-seed",
        }, clear=True):
            config = MessagingConfig()
            assert config.credentials["jwt"] == "test-jwt"
            assert config.credentials["nkey_seed"] == "test-seed"

    def test_mqtt_credentials_from_env(self):
        with patch.dict(os.environ, {
            "MESSAGING_USERNAME": "user",
            "MESSAGING_PASSWORD": "pass",
        }, clear=True):
            config = MessagingConfig()
            assert config.credentials["username"] == "user"
            assert config.credentials["password"] == "pass"

    def test_tls_from_env(self):
        with patch.dict(os.environ, {
            "NATS_TLS_CA_FILE": "/ca.pem",
            "NATS_TLS_CERT_FILE": "/cert.pem",
            "NATS_TLS_KEY_FILE": "/key.pem",
        }, clear=True):
            config = MessagingConfig()
            assert config.tls_config["ca_file"] == "/ca.pem"
            assert config.tls_config["cert_file"] == "/cert.pem"
            assert config.tls_config["key_file"] == "/key.pem"

    def test_tls_messaging_vars_take_precedence(self):
        with patch.dict(os.environ, {
            "MESSAGING_TLS_CA_FILE": "/messaging-ca.pem",
            "MESSAGING_TLS_CERT_FILE": "/messaging-cert.pem",
            "MESSAGING_TLS_KEY_FILE": "/messaging-key.pem",
            "NATS_TLS_CA_FILE": "/nats-ca.pem",
            "NATS_TLS_CERT_FILE": "/nats-cert.pem",
            "NATS_TLS_KEY_FILE": "/nats-key.pem",
        }, clear=True):
            config = MessagingConfig()
            assert config.tls_config["ca_file"] == "/messaging-ca.pem"
            assert config.tls_config["cert_file"] == "/messaging-cert.pem"
            assert config.tls_config["key_file"] == "/messaging-key.pem"

    def test_tls_messaging_vars_alone(self):
        with patch.dict(os.environ, {
            "MESSAGING_TLS_CA_FILE": "/messaging-ca.pem",
        }, clear=True):
            config = MessagingConfig()
            assert config.tls_config["ca_file"] == "/messaging-ca.pem"


class TestMessagingConfigToDict:
    def test_to_dict(self):
        config = MessagingConfig(
            backend="nats",
            servers=["nats://localhost:4222"],
            credentials={"jwt": "x"},
            tls_config={"ca_file": "/ca.pem"},
        )
        d = config.to_dict()
        assert d["backend"] == "nats"
        assert d["servers"] == ["nats://localhost:4222"]
        assert d["credentials"] == {"jwt": "x"}
        assert d["tls_config"] == {"ca_file": "/ca.pem"}

    def test_repr_redacts_credentials(self):
        config = MessagingConfig(credentials={"jwt": "secret"})
        r = repr(config)
        assert "secret" not in r
        assert "REDACTED" in r


class TestCredentialsFile:
    def test_json_credentials(self, tmp_path):
        creds_file = tmp_path / "creds.json"
        creds_file.write_text('{"nats": {"jwt": "file-jwt", "nkey_seed": "file-seed"}}')
        with patch.dict(os.environ, {"NATS_CREDENTIALS_FILE": str(creds_file)}, clear=True):
            config = MessagingConfig()
            assert config.credentials["jwt"] == "file-jwt"
            assert config.credentials["nkey_seed"] == "file-seed"

    def test_nats_creds_format(self, tmp_path):
        creds_file = tmp_path / "user.creds"
        creds_file.write_text(
            "-----BEGIN NATS USER JWT-----\n"
            "eyJhbGciOiJIUzI1NiJ9\n"
            "------END NATS USER JWT------\n"
            "\n"
            "-----BEGIN USER NKEY SEED-----\n"
            "SUACX123\n"
            "------END USER NKEY SEED------\n"
        )
        with patch.dict(os.environ, {"NATS_CREDENTIALS_FILE": str(creds_file)}, clear=True):
            config = MessagingConfig()
            assert config.credentials["jwt"] == "eyJhbGciOiJIUzI1NiJ9"
            assert config.credentials["nkey_seed"] == "SUACX123"
