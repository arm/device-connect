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
    _enlarge_etcd_pool,
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
# TestFleetCache — short-TTL decoded-snapshot reuse (PR #38 review #4)
# ---------------------------------------------------------------------------


class TestFleetCache:
    """list_devices/list_devices_page reuse a short-TTL decoded snapshot so
    a multi-page walk doesn't re-scan + re-decode the whole tenant prefix on
    every page (the ~19,600-decodes-per-poll concern in PR #38 review)."""

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_walk_scans_etcd_once_within_ttl(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        mock_client.get_prefix.return_value = [
            (json.dumps({"device_id": f"d{i}"}),
             {"key": _b64(f"/device-connect/default/devices/d{i}")})
            for i in range(5)
        ]
        mock_etcd3gw.client.return_value = mock_client
        reg = DeviceRegistry(host="localhost", port=2379)

        # A 3-page walk in quick succession (well within the default TTL).
        pages = [reg.list_devices_page("default", offset=o, limit=2)
                 for o in (0, 2, 4)]

        # Only the first page touched etcd; the rest read the cache.
        assert mock_client.get_prefix.call_count == 1
        # ...and pagination still slices correctly off the snapshot.
        assert [len(p[0]) for p in pages] == [2, 2, 1]
        assert pages[-1][1] is None  # last page -> next_offset None

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_register_invalidates_cache(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        mock_client.get_prefix.return_value = []
        mock_etcd3gw.client.return_value = mock_client
        reg = DeviceRegistry(host="localhost", port=2379)

        reg.list_devices("default")  # scan #1, caches the (empty) fleet
        reg.register("default", "cam-1", {"device_id": "cam-1"}, ttl=30)
        reg.list_devices("default")  # must re-scan, not serve a stale snapshot

        assert mock_client.get_prefix.call_count == 2

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_disabled_cache_scans_every_call(self, mock_etcd3gw, monkeypatch):
        monkeypatch.setattr(
            "device_connect_server.registry.service.registry._FLEET_CACHE_TTL",
            0.0,
        )
        mock_client = _make_mock_etcd_client()
        mock_client.get_prefix.return_value = []
        mock_etcd3gw.client.return_value = mock_client
        reg = DeviceRegistry(host="localhost", port=2379)

        reg.list_devices("default")
        reg.list_devices("default")

        assert mock_client.get_prefix.call_count == 2


# ---------------------------------------------------------------------------
# TestListDevicesPage — pagination for large fleets
# ---------------------------------------------------------------------------


class TestListDevicesPage:
    """Verify list_devices_page slices the filtered fleet and reports metadata."""

    @staticmethod
    def _mock_fleet(mock_client, n):
        """Populate mock etcd with n devices ordered by device_id."""
        mock_client.get_prefix.return_value = [
            (
                json.dumps({"device_id": f"dev-{i:04d}"}),
                {"key": _b64(f"/device-connect/default/devices/dev-{i:04d}")},
            )
            for i in range(n)
        ]

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_page_returns_slice_and_next_offset(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        self._mock_fleet(mock_client, 350)
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        page, next_offset, total = reg.list_devices_page(
            "default", offset=0, limit=100,
        )

        assert len(page) == 100
        assert page[0]["device_id"] == "dev-0000"
        assert page[-1]["device_id"] == "dev-0099"
        assert next_offset == 100
        assert total == 350

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_page_final_sets_next_offset_none(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        self._mock_fleet(mock_client, 250)
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        page, next_offset, total = reg.list_devices_page(
            "default", offset=200, limit=100,
        )

        assert len(page) == 50
        assert page[0]["device_id"] == "dev-0200"
        assert next_offset is None
        assert total == 250

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_page_offset_past_end_returns_empty(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        self._mock_fleet(mock_client, 10)
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        page, next_offset, total = reg.list_devices_page(
            "default", offset=50, limit=10,
        )

        assert page == []
        assert next_offset is None
        assert total == 10

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_page_limit_none_returns_remaining(self, mock_etcd3gw):
        mock_client = _make_mock_etcd_client()
        self._mock_fleet(mock_client, 5)
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        page, next_offset, total = reg.list_devices_page(
            "default", offset=0, limit=None,
        )

        assert len(page) == 5
        assert next_offset is None
        assert total == 5

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_page_walks_full_fleet(self, mock_etcd3gw):
        """Looping with next_offset must reconstruct the full fleet."""
        mock_client = _make_mock_etcd_client()
        self._mock_fleet(mock_client, 1400)  # the actual blocker scale
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        gathered = []
        offset = 0
        while True:
            page, next_offset, total = reg.list_devices_page(
                "default", offset=offset, limit=100,
            )
            gathered.extend(page)
            if next_offset is None:
                break
            offset = next_offset

        assert total == 1400
        assert len(gathered) == 1400
        # Order must be stable across pages
        assert [d["device_id"] for d in gathered] == [
            f"dev-{i:04d}" for i in range(1400)
        ]

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_page_respects_filter(self, mock_etcd3gw):
        """device_type filter applies before pagination."""
        mock_client = _make_mock_etcd_client()
        mock_client.get_prefix.return_value = [
            (json.dumps({"device_id": "cam-001", "identity": {"device_type": "camera"}}),
             {"key": _b64("/d")}),
            (json.dumps({"device_id": "arm-001", "identity": {"device_type": "robot"}}),
             {"key": _b64("/d")}),
            (json.dumps({"device_id": "cam-002", "identity": {"device_type": "camera"}}),
             {"key": _b64("/d")}),
        ]
        mock_etcd3gw.client.return_value = mock_client

        reg = DeviceRegistry(host="localhost", port=2379)
        page, next_offset, total = reg.list_devices_page(
            "default", device_type="camera", offset=0, limit=10,
        )

        assert total == 2
        assert len(page) == 2
        assert all(d["identity"]["device_type"] == "camera" for d in page)
        assert next_offset is None


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
        mock_lease.refresh.return_value = 30  # etcd returns the new TTL
        reg = DeviceRegistry(host="localhost", port=2379)
        reg.leases["default/camera-001"] = mock_lease

        reg.refresh("default", "camera-001")

        mock_lease.refresh.assert_called_once()
        # Live lease stays tracked
        assert reg.leases["default/camera-001"] is mock_lease

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
        lease_a.refresh.return_value = 30
        lease_b = MagicMock()
        lease_b.refresh.return_value = 30
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

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_refresh_evicts_expired_lease_no_raise(self, mock_etcd3gw):
        """A stale handle whose lease TTL-expired (refresh() -> -1, no raise)
        is evicted, so has_lease() reports False and the data is gone."""
        mock_client = _make_mock_etcd_client()
        # etcd dropped the lease and its attached key when the TTL expired.
        mock_client.get.return_value = []
        mock_etcd3gw.client.return_value = mock_client

        stale_lease = MagicMock()
        stale_lease.refresh.return_value = -1  # etcd: lease already expired
        reg = DeviceRegistry(host="localhost", port=2379)
        reg.leases["default/cam-001"] = stale_lease

        reg.refresh("default", "cam-001", ttl=15)

        stale_lease.refresh.assert_called_once()
        # Stale handle dropped -> server will fire requestRegistration.
        assert "default/cam-001" not in reg.leases
        assert reg.has_lease("default", "cam-001") is False
        # No phantom lease re-created when etcd has no data.
        mock_client.lease.assert_not_called()

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_refresh_evicts_stale_lease_on_error(self, mock_etcd3gw):
        """A transport/server error from refresh() also evicts the handle."""
        mock_client = _make_mock_etcd_client()
        mock_client.get.return_value = []
        mock_etcd3gw.client.return_value = mock_client

        stale_lease = MagicMock()
        stale_lease.refresh.side_effect = RuntimeError("lease not found")
        reg = DeviceRegistry(host="localhost", port=2379)
        reg.leases["default/cam-001"] = stale_lease

        reg.refresh("default", "cam-001", ttl=15)

        assert "default/cam-001" not in reg.leases
        assert reg.has_lease("default", "cam-001") is False

    @patch("device_connect_server.registry.service.registry.etcd3gw")
    def test_refresh_recovers_when_data_survives_lease(self, mock_etcd3gw):
        """If the stale lease died but the device doc is still in etcd,
        refresh() re-issues a lease and re-stores the data."""
        mock_client = _make_mock_etcd_client()
        existing = {"device_id": "cam-001", "status": {"online": True}}
        mock_client.get.return_value = [json.dumps(existing)]
        new_lease = MagicMock()
        mock_client.lease.return_value = new_lease
        mock_etcd3gw.client.return_value = mock_client

        stale_lease = MagicMock()
        stale_lease.refresh.return_value = -1
        reg = DeviceRegistry(host="localhost", port=2379)
        reg.leases["default/cam-001"] = stale_lease

        reg.refresh("default", "cam-001", ttl=15)

        mock_client.lease.assert_called_once_with(ttl=15)
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


# ---------------------------------------------------------------------------
# TestEnlargeEtcdPool
# ---------------------------------------------------------------------------


class TestEnlargeEtcdPool:
    """The fix mounts an oversized urllib3 HTTPAdapter onto the etcd3gw
    client's underlying ``requests.Session`` so the registry doesn't
    bottleneck on the default 10-socket pool under a registration herd.
    The fallback path (etcd3gw stops exposing ``client.session``) must
    warn loudly rather than silently regress to the old pool size."""

    def test_mounts_adapter_on_existing_session(self):
        session = MagicMock()
        client = MagicMock()
        client.session = session

        _enlarge_etcd_pool(client, pool_size=64)

        # Both http:// and https:// must be mounted so we don't leave a
        # surviving small-pool adapter on either scheme.
        scheme_args = [call.args[0] for call in session.mount.call_args_list]
        assert "http://" in scheme_args
        assert "https://" in scheme_args

    def test_logs_warning_when_session_missing(self, caplog):
        # ``spec=[]`` makes ``hasattr(client, "session")`` return False
        # without raising — simulating a future etcd3gw refactor that
        # renames or hides the session attribute.
        client = MagicMock(spec=[])

        with caplog.at_level("WARNING", logger="device_connect_server.registry.service.registry"):
            _enlarge_etcd_pool(client, pool_size=64)

        assert any(
            "session" in rec.message and "urllib3 default" in rec.message
            for rec in caplog.records
        ), f"expected pool-fallback warning, got: {[r.message for r in caplog.records]}"
