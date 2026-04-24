# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for device_connect_server.registry.service.registry — DeviceRegistry.

The ``etcd3gw`` client is fully mocked so no real etcd server is needed.
We inject a mock ``etcd3gw`` into ``sys.modules`` *before* importing the
registry module so that the module-level ``import etcd3gw`` succeeds even
when the real package is not installed.
"""

import base64
import json
import sys
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Inject mock etcd3gw into sys.modules BEFORE importing the registry module.
# This allows the module-level ``import etcd3gw`` to succeed.
# ---------------------------------------------------------------------------
_mock_etcd3gw = MagicMock()
sys.modules.setdefault("etcd3gw", _mock_etcd3gw)

from device_connect_server.registry.service.registry import (  # noqa: E402
    DeviceRegistry,
    _kv_key,
    register,
    refresh,
    has_lease,
    list_devices,
    update_status,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_etcd_client():
    """Return a mock etcd3gw client with sane defaults."""
    client = MagicMock()
    client.put = MagicMock()
    client.get = MagicMock(return_value=[])
    client.get_prefix = MagicMock(return_value=[])
    client.lease = MagicMock()
    return client


def _b64(s: str) -> str:
    """Base64-encode a string (to simulate etcd3gw key encoding)."""
    return base64.b64encode(s.encode("utf-8")).decode("utf-8")


SAMPLE_DEVICE = {
    "device_id": "camera-001",
    "device_type": "camera",
    "location": "lab-A",
    "status": {"online": True},
}


# ---------------------------------------------------------------------------
# TestDeviceRegistryInit
# ---------------------------------------------------------------------------


class TestDeviceRegistryInit:
    """Constructor with host/port."""

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_constructor_stores_host_port(self, mock_etcd3gw):
        mock_etcd3gw.client.return_value = _make_mock_etcd_client()
        reg = DeviceRegistry(host="etcd.local", port=2379)
        assert reg.host == "etcd.local"
        assert reg.port == 2379

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_constructor_creates_etcd_client(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        mock_etcd3gw.client.return_value = mock_client
        reg = DeviceRegistry(host="etcd.local", port=2379)
        mock_etcd3gw.client.assert_called_once_with(host="etcd.local", port=2379)
        assert reg.client is mock_client

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_constructor_initializes_empty_leases(self, mock_etcd3gw):
        mock_etcd3gw.client.return_value = _make_mock_etcd_client()
        reg = DeviceRegistry(host="localhost", port=2379)
        assert reg.leases == {}

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_key_format(self, mock_etcd3gw):
        mock_etcd3gw.client.return_value = _make_mock_etcd_client()
        reg = DeviceRegistry(host="localhost", port=2379)
        assert reg._key("default", "cam-001") == "/device-connect/default/devices/cam-001"

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_lease_key_format(self, mock_etcd3gw):
        mock_etcd3gw.client.return_value = _make_mock_etcd_client()
        reg = DeviceRegistry(host="localhost", port=2379)
        assert reg._lease_key("default", "cam-001") == "default/cam-001"


# ---------------------------------------------------------------------------
# TestRegisterDevice
# ---------------------------------------------------------------------------


class TestRegisterDevice:
    """Verify register() creates lease, stores payload, and tracks lease."""

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_register_creates_lease_with_ttl(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        mock_lease = MagicMock()
        mock_client.lease.return_value = mock_lease
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        reg.register("default", "camera-001", SAMPLE_DEVICE, ttl=30)

        mock_client.lease.assert_called_once_with(ttl=30)

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_register_puts_json_payload(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        mock_lease = MagicMock()
        mock_client.lease.return_value = mock_lease
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        reg.register("default", "camera-001", SAMPLE_DEVICE, ttl=30)

        expected_key = "/device-connect/default/devices/camera-001"
        expected_value = json.dumps(SAMPLE_DEVICE)
        mock_client.put.assert_called_once_with(
            expected_key, expected_value, lease=mock_lease
        )

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_register_stores_lease(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        mock_lease = MagicMock()
        mock_client.lease.return_value = mock_lease
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        reg.register("default", "camera-001", SAMPLE_DEVICE, ttl=30)

        assert reg.leases["default/camera-001"] is mock_lease

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_register_multiple_devices(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        mock_client.lease.return_value = MagicMock()
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        reg.register("default", "cam-001", {"id": "cam-001"}, ttl=30)
        reg.register("default", "cam-002", {"id": "cam-002"}, ttl=60)

        assert "default/cam-001" in reg.leases
        assert "default/cam-002" in reg.leases
        assert mock_client.put.call_count == 2

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_register_multi_tenant(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        mock_client.lease.return_value = MagicMock()
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        reg.register("tenant-a", "cam-001", {"id": "cam-001"}, ttl=30)
        reg.register("tenant-b", "cam-001", {"id": "cam-001"}, ttl=30)

        assert "tenant-a/cam-001" in reg.leases
        assert "tenant-b/cam-001" in reg.leases


# ---------------------------------------------------------------------------
# TestListDevices
# ---------------------------------------------------------------------------


class TestListDevices:
    """Verify prefix query and JSON parsing."""

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_list_devices_uses_prefix(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        mock_client.get_prefix.return_value = []
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        reg.list_devices("default")

        mock_client.get_prefix.assert_called_once_with(
            "/device-connect/default/devices/"
        )

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_list_devices_returns_parsed_json(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        device1 = {"device_id": "cam-001", "type": "camera"}
        device2 = {"device_id": "arm-001", "type": "robot"}
        mock_client.get_prefix.return_value = [
            (json.dumps(device1), {"key": _b64("/device-connect/default/devices/cam-001")}),
            (json.dumps(device2), {"key": _b64("/device-connect/default/devices/arm-001")}),
        ]
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        devices = reg.list_devices("default")

        assert len(devices) == 2
        assert devices[0]["device_id"] == "cam-001"
        assert devices[1]["device_id"] == "arm-001"

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_list_devices_empty(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        mock_client.get_prefix.return_value = []
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        devices = reg.list_devices("default")

        assert devices == []

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_list_devices_skips_invalid_json(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        valid = {"device_id": "cam-001"}
        mock_client.get_prefix.return_value = [
            (json.dumps(valid), {"key": _b64("/device-connect/default/devices/cam-001")}),
            ("not-json{{{", {"key": _b64("/device-connect/default/devices/broken")}),
        ]
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        devices = reg.list_devices("default")

        assert len(devices) == 1
        assert devices[0]["device_id"] == "cam-001"

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_list_devices_multi_tenant_isolation(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        mock_client.get_prefix.return_value = []
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        reg.list_devices("tenant-a")
        reg.list_devices("tenant-b")

        calls = mock_client.get_prefix.call_args_list
        assert calls[0] == call("/device-connect/tenant-a/devices/")
        assert calls[1] == call("/device-connect/tenant-b/devices/")


# ---------------------------------------------------------------------------
# TestGetDevice (via update_status reading pattern)
# ---------------------------------------------------------------------------


class TestGetDevice:
    """Single device lookup; missing device returns None / empty."""

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_get_device_found(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        mock_client.get.return_value = [json.dumps(SAMPLE_DEVICE)]
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        results = reg.client.get(reg._key("default", "camera-001"))

        assert len(results) == 1
        doc = json.loads(results[0])
        assert doc["device_id"] == "camera-001"

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_get_device_missing_returns_empty(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        mock_client.get.return_value = []
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        results = reg.client.get(reg._key("default", "nonexistent"))

        assert results == []

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_update_status_on_missing_device_is_noop(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        mock_client.get.return_value = []
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        reg.update_status("default", "nonexistent", {"online": False})

        mock_client.put.assert_not_called()

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_update_status_merges_status(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        existing = {"device_id": "cam-001", "status": {"online": True, "battery": 80}}
        mock_client.get.return_value = [json.dumps(existing)]
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        mock_lease = MagicMock()
        reg.leases["default/cam-001"] = mock_lease

        reg.update_status("default", "cam-001", {"online": False})

        put_call = mock_client.put.call_args
        stored = json.loads(put_call[0][1])
        assert stored["status"]["online"] is False
        assert stored["status"]["battery"] == 80
        assert put_call[1]["lease"] is mock_lease

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_update_status_without_lease(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        existing = {"device_id": "cam-001", "status": {"online": True}}
        mock_client.get.return_value = [json.dumps(existing)]
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        reg.update_status("default", "cam-001", {"online": False})

        put_call = mock_client.put.call_args
        stored = json.loads(put_call[0][1])
        assert stored["status"]["online"] is False
        assert "lease" not in put_call[1] or put_call[1].get("lease") is None

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_update_status_skips_write_when_unchanged(self, mock_etcd3gw):
        """Heartbeat status identical to stored status should not write."""
        mock_client = _make_mock_etcd_client()
        existing = {"device_id": "cam-001", "status": {"online": True, "battery": 80}}
        mock_client.get.return_value = [json.dumps(existing)]
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        reg.update_status("default", "cam-001", {"online": True})

        mock_client.put.assert_not_called()

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_update_status_skips_empty_status(self, mock_etcd3gw):
        """Empty status dict should short-circuit without reading etcd."""
        mock_client = _make_mock_etcd_client()
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        reg.update_status("default", "cam-001", {})

        mock_client.get.assert_not_called()
        mock_client.put.assert_not_called()

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_update_status_writes_new_field(self, mock_etcd3gw):
        """A new field not in existing status should trigger a write."""
        mock_client = _make_mock_etcd_client()
        existing = {"device_id": "cam-001", "status": {"online": True}}
        mock_client.get.return_value = [json.dumps(existing)]
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        mock_lease = MagicMock()
        reg.leases["default/cam-001"] = mock_lease

        reg.update_status("default", "cam-001", {"battery": 80})

        put_call = mock_client.put.call_args
        stored = json.loads(put_call[0][1])
        assert stored["status"]["online"] is True
        assert stored["status"]["battery"] == 80


# ---------------------------------------------------------------------------
# TestRefreshHeartbeat
# ---------------------------------------------------------------------------


class TestRefreshHeartbeat:
    """Verify lease refresh behavior."""

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_refresh_calls_lease_refresh(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        mock_etcd3gw.client.return_value = mock_client

        mock_lease = MagicMock()
        reg = DeviceRegistry(host="localhost", port=2379)
        reg.leases["default/camera-001"] = mock_lease

        reg.refresh("default", "camera-001")

        mock_lease.refresh.assert_called_once()

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_refresh_noop_for_unknown_device(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        # Should not raise
        reg.refresh("default", "unknown-device")

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_refresh_multi_tenant_isolation(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        mock_etcd3gw.client.return_value = mock_client

        lease_a = MagicMock()
        lease_b = MagicMock()
        reg = DeviceRegistry(host="localhost", port=2379)
        reg.leases["tenant-a/cam-001"] = lease_a
        reg.leases["tenant-b/cam-001"] = lease_b

        reg.refresh("tenant-a", "cam-001")

        lease_a.refresh.assert_called_once()
        lease_b.refresh.assert_not_called()


# ---------------------------------------------------------------------------
# TestHasLease
# ---------------------------------------------------------------------------


class TestHasLease:
    """Verify has_lease() on a DeviceRegistry instance."""

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_has_lease_exists(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        mock_client.lease.return_value = MagicMock()
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        reg.register("default", "cam-001", {"id": "cam-001"}, ttl=30)

        assert reg.has_lease("default", "cam-001") is True

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_has_lease_missing(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)

        assert reg.has_lease("default", "unknown-device") is False


class TestModuleHasLease:
    """Verify the module-level has_lease() wrapper."""

    @patch("device_connect_server.registry.service.registry._REGISTRY")
    def test_module_has_lease(self, mock_reg):
        mock_reg.has_lease.return_value = True
        result = has_lease("default", "cam-001")
        mock_reg.has_lease.assert_called_once_with("default", "cam-001")
        assert result is True


# ---------------------------------------------------------------------------
# TestRefreshLeaseRecovery
# ---------------------------------------------------------------------------


class TestRefreshLeaseRecovery:
    """Verify refresh() recovers a lost lease from etcd data."""

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_refresh_lease_recovery(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        existing = {"device_id": "cam-001", "status": {"online": True}}
        mock_client.get.return_value = [json.dumps(existing)]
        new_lease = MagicMock()
        mock_client.lease.return_value = new_lease
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        # No lease in leases dict — simulates service restart
        assert "default/cam-001" not in reg.leases

        reg.refresh("default", "cam-001", ttl=15)

        # A new lease should have been created
        mock_client.lease.assert_called_once_with(ttl=15)
        # Data should have been re-stored with the new lease
        expected_key = "/device-connect/default/devices/cam-001"
        mock_client.put.assert_called_once_with(
            expected_key, json.dumps(existing), lease=new_lease,
        )
        # The lease should now be tracked
        assert reg.leases["default/cam-001"] is new_lease


# ---------------------------------------------------------------------------
# TestModuleLevelHelpers
# ---------------------------------------------------------------------------


class TestModuleLevelHelpers:
    """Verify module-level convenience functions delegate to _REGISTRY."""

    @patch("device_connect_server.registry.service.registry._REGISTRY")
    def test_module_register(self, mock_reg):
        register("default", "cam-001", {"id": "cam-001"}, ttl=30)
        mock_reg.register.assert_called_once_with(
            "default", "cam-001", {"id": "cam-001"}, 30
        )

    @patch("device_connect_server.registry.service.registry._REGISTRY")
    def test_module_refresh(self, mock_reg):
        refresh("default", "cam-001")
        mock_reg.refresh.assert_called_once_with("default", "cam-001", ttl=None)

    @patch("device_connect_server.registry.service.registry._REGISTRY")
    def test_module_list_devices(self, mock_reg):
        mock_reg.list_devices.return_value = [{"id": "cam-001"}]
        result = list_devices("default")
        mock_reg.list_devices.assert_called_once_with("default", device_type=None, location=None)
        assert result == [{"id": "cam-001"}]

    @patch("device_connect_server.registry.service.registry._REGISTRY")
    def test_module_update_status(self, mock_reg):
        update_status("default", "cam-001", {"online": False})
        mock_reg.update_status.assert_called_once_with(
            "default", "cam-001", {"online": False}
        )


# ---------------------------------------------------------------------------
# TestKvKeyHelper
# ---------------------------------------------------------------------------


class TestKvKeyHelper:
    """Verify _kv_key base64 decoding."""

    def test_decodes_base64_key(self):
        encoded = _b64("/device-connect/default/devices/cam-001")
        result = _kv_key({"key": encoded})
        assert result == "/device-connect/default/devices/cam-001"

    def test_returns_raw_on_decode_failure(self):
        result = _kv_key({"key": "not-valid-b64!!!"})
        assert isinstance(result, str)

    def test_returns_empty_when_key_missing(self):
        result = _kv_key({})
        assert result == ""
