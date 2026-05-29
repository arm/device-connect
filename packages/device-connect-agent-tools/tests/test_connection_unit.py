# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for device_connect_agent_tools.connection module.

Tests connection management, config resolution, and RPC helpers
using mocks (no real NATS required).
"""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

from device_connect_agent_tools import connection as conn_mod


# ── Config resolution (via MessagingConfig + auto-discovery) ─────


class TestDeviceConnectConnectionConfig:
    """Test that DeviceConnection resolves config correctly."""

    @patch.object(conn_mod, "_auto_discover_tls", return_value=None)
    @patch.object(conn_mod, "_auto_discover_credentials", return_value=None)
    @patch("device_connect_agent_tools.connection.MessagingConfig")
    def test_explicit_url_passed_to_config(self, MockConfig, _ad_creds, _ad_tls):
        mock_cfg = MagicMock()
        mock_cfg.servers = ["nats://myhost:4222"]
        mock_cfg.credentials = {"jwt": "j", "nkey_seed": "s"}
        mock_cfg.tls_config = None
        MockConfig.return_value = mock_cfg

        conn = conn_mod.DeviceConnection(nats_url="nats://myhost:4222")
        MockConfig.assert_called_once_with(
            servers=["nats://myhost:4222"],
            credentials=None,
            tls_config=None,
        )
        assert conn._servers == ["nats://myhost:4222"]
        conn.close()

    @patch.object(conn_mod, "_auto_discover_tls", return_value=None)
    @patch.object(conn_mod, "_auto_discover_credentials", return_value={"jwt": "disc-j", "nkey_seed": "disc-s"})
    @patch("device_connect_agent_tools.connection.MessagingConfig")
    def test_autodiscovery_fills_credentials(self, MockConfig, ad_creds, _ad_tls):
        mock_cfg = MagicMock()
        mock_cfg.servers = ["nats://localhost:4222"]
        mock_cfg.credentials = None  # MessagingConfig found nothing
        mock_cfg.tls_config = None
        MockConfig.return_value = mock_cfg

        conn = conn_mod.DeviceConnection()
        assert conn._credentials == {"jwt": "disc-j", "nkey_seed": "disc-s"}
        ad_creds.assert_called_once()
        conn.close()

    @patch.object(conn_mod, "_auto_discover_tls", return_value={"ca_file": "/discovered/ca.pem"})
    @patch.object(conn_mod, "_auto_discover_credentials", return_value=None)
    @patch("device_connect_agent_tools.connection.MessagingConfig")
    def test_autodiscovery_fills_tls_and_upgrades_server(self, MockConfig, _ad_creds, ad_tls):
        mock_cfg = MagicMock()
        mock_cfg.servers = ["nats://localhost:4222"]
        mock_cfg.credentials = None
        mock_cfg.tls_config = None
        MockConfig.return_value = mock_cfg

        with patch.dict(os.environ, {}, clear=True):
            conn = conn_mod.DeviceConnection()
            assert conn._tls_config == {"ca_file": "/discovered/ca.pem"}
            # Should upgrade to tls:// when TLS is discovered and no explicit URL
            assert conn._servers == ["tls://localhost:4222"]
            conn.close()

    @patch.object(conn_mod, "_auto_discover_tls", return_value=None)
    @patch.object(conn_mod, "_auto_discover_credentials", return_value=None)
    @patch("device_connect_agent_tools.connection.MessagingConfig")
    def test_config_skips_autodiscovery_when_already_set(self, MockConfig, ad_creds, ad_tls):
        mock_cfg = MagicMock()
        mock_cfg.servers = ["nats://localhost:4222"]
        mock_cfg.credentials = {"jwt": "from-env", "nkey_seed": "s"}
        mock_cfg.tls_config = {"ca_file": "/from-env/ca.pem"}
        MockConfig.return_value = mock_cfg

        conn = conn_mod.DeviceConnection()
        # Auto-discovery should NOT be called since MessagingConfig already resolved values
        ad_creds.assert_not_called()
        ad_tls.assert_not_called()
        assert conn._credentials == {"jwt": "from-env", "nkey_seed": "s"}
        assert conn._tls_config == {"ca_file": "/from-env/ca.pem"}
        conn.close()

    @patch.object(conn_mod, "_auto_discover_tls", return_value=None)
    @patch.object(conn_mod, "_auto_discover_credentials", return_value=None)
    def test_portal_bundle_prefers_local_zenoh_route(self, _ad_creds, _ad_tls, tmp_path):
        bundle = tmp_path / "agent.creds.json"
        bundle.write_text(json.dumps({
            "tenant": "lab-a",
            "device_id": "robot-001",
            "nats": {
                "urls": ["nats://portal.example:4222"],
                "jwt": "portal-jwt",
                "nkey_seed": "portal-seed",
            },
            "local": {
                "routes": ["tls/192.168.1.42:7447"],
                "tls": {
                    "ca_file": "/tmp/ca.pem",
                    "cert_file": "/tmp/client.pem",
                    "key_file": "/tmp/client-key.pem",
                },
            },
        }))

        with patch.dict(os.environ, {"DEVICE_CONNECT_PORTAL_CREDENTIALS_FILE": str(bundle)}, clear=True):
            conn = conn_mod.DeviceConnection()

        assert conn.zone == "lab-a"
        assert conn._backend == "zenoh"
        assert conn._servers == ["tls/192.168.1.42:7447"]
        assert conn._tls_config == {
            "ca_file": "/tmp/ca.pem",
            "cert_file": "/tmp/client.pem",
            "key_file": "/tmp/client-key.pem",
        }
        assert conn._d2d_mode is True
        assert conn._fallback_config["servers"] == ["nats://portal.example:4222"]
        conn.close()

    @patch.object(conn_mod, "_auto_discover_tls", return_value=None)
    @patch.object(conn_mod, "_auto_discover_credentials", return_value=None)
    def test_portal_bundle_can_disable_local_preference(self, _ad_creds, _ad_tls, tmp_path):
        bundle = tmp_path / "agent.creds.json"
        bundle.write_text(json.dumps({
            "tenant": "lab-a",
            "nats": {"urls": ["nats://portal.example:4222"], "jwt": "j", "nkey_seed": "s"},
            "local_routes": ["tls/192.168.1.42:7447"],
            "zenoh": {"tls": {"ca_file": "/tmp/ca.pem"}},
        }))

        env = {
            "DEVICE_CONNECT_PORTAL_CREDENTIALS_FILE": str(bundle),
            "DEVICE_CONNECT_PREFER_LOCAL": "false",
        }
        with patch.dict(os.environ, env, clear=True):
            conn = conn_mod.DeviceConnection()

        assert conn._backend == "nats"
        assert conn._servers == ["nats://portal.example:4222"]
        assert conn._credentials == {"jwt": "j", "nkey_seed": "s"}
        assert conn._d2d_mode is False
        conn.close()

    @patch.object(conn_mod, "_auto_discover_tls", return_value=None)
    @patch.object(conn_mod, "_auto_discover_credentials", return_value=None)
    def test_local_route_connect_failure_falls_back_to_portal(self, _ad_creds, _ad_tls, tmp_path):
        bundle = tmp_path / "agent.creds.json"
        bundle.write_text(json.dumps({
            "tenant": "lab-a",
            "nats": {
                "urls": ["nats://portal.example:4222"],
                "jwt": "portal-jwt",
                "nkey_seed": "portal-seed",
            },
            "local": {
                "routes": ["tls/192.168.1.42:7447"],
                "tls": {"ca_file": "/tmp/local-ca.pem"},
            },
        }))

        local_client = MagicMock()
        local_client.connect = AsyncMock(side_effect=RuntimeError("local route unavailable"))
        local_client.close = AsyncMock()
        portal_client = MagicMock()
        portal_client.connect = AsyncMock()
        portal_client.close = AsyncMock()
        registry = MagicMock()

        env = {"DEVICE_CONNECT_PORTAL_CREDENTIALS_FILE": str(bundle)}
        with patch.dict(os.environ, env, clear=True), \
             patch("device_connect_agent_tools.connection.create_client",
                   side_effect=[local_client, portal_client]) as create_client, \
             patch("device_connect_agent_tools.connection._SDKRegistryClient",
                   return_value=registry) as registry_client:
            conn = conn_mod.DeviceConnection()
            conn.connect()

        assert [call.kwargs["backend"] for call in create_client.call_args_list] == ["zenoh", "nats"]
        local_client.connect.assert_awaited_once_with(
            servers=["tls/192.168.1.42:7447"],
            credentials=None,
            tls_config={"ca_file": "/tmp/local-ca.pem"},
        )
        local_client.close.assert_awaited_once()
        portal_client.connect.assert_awaited_once_with(
            servers=["nats://portal.example:4222"],
            credentials={"jwt": "portal-jwt", "nkey_seed": "portal-seed"},
            tls_config=None,
        )
        registry_client.assert_called_once_with(
            portal_client,
            tenant="lab-a",
            timeout=30.0,
            cache_ttl=30.0,
        )
        assert conn._backend == "nats"
        assert conn._servers == ["nats://portal.example:4222"]
        assert conn._credentials == {"jwt": "portal-jwt", "nkey_seed": "portal-seed"}
        assert conn._tls_config is None
        assert conn._using_local_route is False
        assert conn._d2d_mode is False
        assert conn._fallback_config is None
        assert conn._provider is registry
        conn.close()


# ── Auto-discovery helpers ───────────────────────────────────────


class TestAutoDiscovery:
    def test_find_device_connect_root_from_cwd(self, tmp_path):
        """Should find root when security_infra/credentials exists."""
        (tmp_path / "security_infra" / "credentials").mkdir(parents=True)
        with patch("device_connect_agent_tools.connection.Path.cwd", return_value=tmp_path):
            assert conn_mod._find_device_connect_root() == tmp_path

    def test_find_device_connect_root_core_child(self, tmp_path):
        """Should find core/ child when core/security_infra/credentials exists."""
        (tmp_path / "core" / "security_infra" / "credentials").mkdir(parents=True)
        with patch("device_connect_agent_tools.connection.Path.cwd", return_value=tmp_path):
            assert conn_mod._find_device_connect_root() == tmp_path / "core"

    def test_find_device_connect_root_not_found(self, tmp_path):
        with patch("device_connect_agent_tools.connection.Path.cwd", return_value=tmp_path):
            assert conn_mod._find_device_connect_root() is None

    def test_auto_discover_credentials_found(self, tmp_path):
        creds_dir = tmp_path / "security_infra" / "credentials"
        creds_dir.mkdir(parents=True)
        creds_file = creds_dir / "orchestrator.creds.json"
        creds_file.write_text(json.dumps({"nats": {"jwt": "j", "nkey_seed": "s"}}))

        with patch.object(conn_mod, "_find_device_connect_root", return_value=tmp_path):
            with patch("device_connect_agent_tools.connection.MessagingConfig._load_credentials_file",
                       return_value={"jwt": "j", "nkey_seed": "s"}) as mock_load:
                result = conn_mod._auto_discover_credentials()
                assert result == {"jwt": "j", "nkey_seed": "s"}
                mock_load.assert_called_once_with(str(creds_file))

    def test_auto_discover_tls_found(self, tmp_path):
        certs_dir = tmp_path / "security_infra" / "certs"
        certs_dir.mkdir(parents=True)
        (certs_dir / "ca-cert.pem").write_text("PEM DATA")

        with patch.object(conn_mod, "_find_device_connect_root", return_value=tmp_path):
            result = conn_mod._auto_discover_tls()
            assert result == {"ca_file": str(certs_dir / "ca-cert.pem")}

    def test_normalize_local_zenoh_dict(self):
        cfg = conn_mod.normalize_local_zenoh_dict(
            {"routes": ["tcp/10.0.0.5:7447"], "tls": {"ca_file": "/tmp/ca.pem"}},
        )
        assert cfg == {
            "backend": "zenoh",
            "servers": ["tcp/10.0.0.5:7447"],
            "credentials": None,
            "tls": {"ca_file": "/tmp/ca.pem"},
        }

    def test_collect_local_route_candidates_from_devices(self):
        devices = [
            {
                "device_id": "a",
                "status": {"local_zenoh": {"routes": ["tcp/10.0.0.1:7447"]}},
            },
            {
                "device_id": "b",
                "status": {"local_zenoh": {"routes": ["tcp/10.0.0.1:7447"]}},
            },
            {
                "device_id": "c",
                "status": {"local_zenoh": {"routes": ["tcp/10.0.0.2:7447"]}},
            },
        ]
        candidates = conn_mod.collect_local_route_candidates_from_devices(devices)
        assert len(candidates) == 2
        assert {tuple(c["servers"]) for c in candidates} == {
            ("tcp/10.0.0.1:7447",),
            ("tcp/10.0.0.2:7447",),
        }

    @patch.object(conn_mod, "_auto_discover_tls", return_value=None)
    @patch.object(conn_mod, "_auto_discover_credentials", return_value=None)
    def test_portal_bundle_registry_local_discovery_flag(self, _ad_creds, _ad_tls, tmp_path):
        bundle = tmp_path / "agent.creds.json"
        bundle.write_text(json.dumps({
            "tenant": "lab-a",
            "nats": {"urls": ["nats://portal.example:4222"], "jwt": "j", "nkey_seed": "s"},
        }))

        with patch.dict(os.environ, {"DEVICE_CONNECT_PORTAL_CREDENTIALS_FILE": str(bundle)}, clear=True):
            conn = conn_mod.DeviceConnection()

        assert conn._registry_local_discovery is True
        assert conn._stored_portal_cfg["servers"] == ["nats://portal.example:4222"]
        assert conn._fallback_config["servers"] == ["nats://portal.example:4222"]
        conn.close()

    def test_load_portal_credentials_file_splits_portal_and_local_routes(self, tmp_path):
        bundle = tmp_path / "agent.creds.json"
        bundle.write_text(json.dumps({
            "tenant": "lab-a",
            "nats": {"urls": ["nats://portal.example:4222"], "jwt": "j", "nkey_seed": "s"},
            "local_routes": ["tls/192.168.1.42:7447"],
            "zenoh": {"tls": {"ca_file": "/tmp/ca.pem"}},
        }))

        result = conn_mod.load_portal_credentials_file(bundle)

        assert result == {
            "tenant": "lab-a",
            "portal": {
                "backend": "nats",
                "servers": ["nats://portal.example:4222"],
                "credentials": {"jwt": "j", "nkey_seed": "s"},
                "tls": None,
            },
            "local": {
                "backend": "zenoh",
                "servers": ["tls/192.168.1.42:7447"],
                "credentials": None,
                "tls": {"ca_file": "/tmp/ca.pem"},
                "device_id": None,
                "expires_at": None,
            },
        }


# ── Singleton connect/disconnect ──────────────────────────────────


class TestConnectDisconnect:
    def setup_method(self):
        """Reset the module-level singleton before each test."""
        conn_mod._connection = None

    def teardown_method(self):
        conn_mod._connection = None

    def test_disconnect_when_not_connected(self):
        """disconnect() should be safe when not connected."""
        conn_mod.disconnect()  # Should not raise

    def test_connect_sets_singleton(self):
        mock_conn = MagicMock()
        with patch.object(conn_mod, "DeviceConnection", return_value=mock_conn):
            conn_mod.connect(nats_url="nats://mock:4222")
            assert conn_mod._connection is mock_conn
            mock_conn.connect.assert_called_once()

    def test_connect_idempotent(self):
        mock_conn = MagicMock()
        conn_mod._connection = mock_conn
        # Second call should not create a new connection
        conn_mod.connect(nats_url="nats://mock:4222")
        assert conn_mod._connection is mock_conn

    def test_disconnect_clears_singleton(self):
        mock_conn = MagicMock()
        conn_mod._connection = mock_conn
        conn_mod.disconnect()
        assert conn_mod._connection is None
        mock_conn.close.assert_called_once()

    def test_get_connection_auto_connects(self):
        mock_conn = MagicMock()
        with patch.object(conn_mod, "connect") as mock_connect:
            # First call: _connection is None, so connect() is called
            conn_mod._connection = None

            def set_conn(**kwargs):
                conn_mod._connection = mock_conn
            mock_connect.side_effect = set_conn

            result = conn_mod.get_connection()
            mock_connect.assert_called_once()
            assert result is mock_conn


# ── Flatten device helper ─────────────────────────────────────────


class TestFlattenDevice:
    def test_basic_flatten(self):
        raw = {
            "device_id": "cam-001",
            "identity": {"device_type": "camera"},
            "status": {"location": "lab-1"},
            "capabilities": {
                "functions": [{"name": "capture"}],
                "events": [{"name": "captured"}],
            },
        }
        result = conn_mod.flatten_device(raw)
        assert result["device_id"] == "cam-001"
        assert result["device_type"] == "camera"
        assert result["location"] == "lab-1"
        assert len(result["functions"]) == 1
        assert len(result["events"]) == 1
        assert "capabilities" not in result

    def test_top_level_takes_precedence(self):
        raw = {
            "device_id": "x",
            "device_type": "top-type",
            "location": "top-loc",
            "identity": {"device_type": "nested-type"},
            "status": {"location": "nested-loc"},
            "capabilities": {},
        }
        result = conn_mod.flatten_device(raw)
        assert result["device_type"] == "top-type"
        assert result["location"] == "top-loc"

    def test_empty_raw(self):
        result = conn_mod.flatten_device({})
        assert result["device_id"] is None
        assert result["device_type"] is None
        assert result["functions"] == []
        assert result["events"] == []


# ── parse_event_payload: non-dict params (regression for fuzz finding) ─


class TestParseEventPayloadNonDictParams:
    """JSON-RPC ``params`` may be omitted, ``null``, an object, or an array
    per the spec. Earlier ``parse_event_payload`` chained ``.get`` and
    crashed when ``params`` was explicitly present but non-dict (e.g.
    ``null``). These tests pin the normalize-to-dict behavior.
    """

    def test_params_explicit_null(self):
        data = json.dumps({"method": "evt", "params": None}).encode()
        result = conn_mod.parse_event_payload(data)
        assert result == {"device_id": "unknown", "event_name": "evt", "params": {}}

    def test_params_array(self):
        data = json.dumps({"method": "evt", "params": [1, 2, 3]}).encode()
        result = conn_mod.parse_event_payload(data)
        assert result["params"] == {}
        assert result["device_id"] == "unknown"

    def test_params_scalar(self):
        data = json.dumps({"method": "evt", "params": "string"}).encode()
        result = conn_mod.parse_event_payload(data)
        assert result["params"] == {}

    def test_params_missing(self):
        data = json.dumps({"method": "evt"}).encode()
        result = conn_mod.parse_event_payload(data)
        assert result == {"device_id": "unknown", "event_name": "evt", "params": {}}

    def test_params_happy_path(self):
        data = json.dumps(
            {"method": "evt", "params": {"device_id": "cam-001", "x": 1}}
        ).encode()
        result = conn_mod.parse_event_payload(data)
        assert result["device_id"] == "cam-001"
        assert result["params"] == {"device_id": "cam-001", "x": 1}
