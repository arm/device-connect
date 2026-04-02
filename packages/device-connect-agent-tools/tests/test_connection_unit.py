"""Unit tests for device_connect_agent_tools.connection module.

Tests connection management, config resolution, and RPC helpers
using mocks (no real NATS required).
"""

import json
import os
from unittest.mock import MagicMock, patch

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
