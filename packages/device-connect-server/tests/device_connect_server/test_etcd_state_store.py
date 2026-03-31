"""Unit tests for EtcdStateStore.

Tests can run as integration tests against a real etcd instance
(requires Docker) or as unit tests with mocked etcd3gw client.
"""

import base64
import json
from unittest.mock import MagicMock

import pytest

from device_connect_server.state.etcd_store import EtcdStateStore


class TestEtcdStateStoreInit:
    """Tests for initialization."""

    def test_default_config(self):
        store = EtcdStateStore()
        assert store._host == "localhost"
        assert store._port == 2379
        assert store._key_prefix == "/device-connect/state/"
        assert store._client is None

    def test_custom_config(self):
        store = EtcdStateStore(host="etcd.local", port=2380, key_prefix="/test/")
        assert store._host == "etcd.local"
        assert store._port == 2380
        assert store._key_prefix == "/test/"

    def test_full_key(self):
        store = EtcdStateStore(key_prefix="/device-connect/state/")
        assert store._full_key("experiments/EXP-001") == "/device-connect/state/experiments/EXP-001"

    def test_lock_key(self):
        store = EtcdStateStore()
        assert store._lock_key("device_locks/felix-1") == "/device-connect/locks/device_locks/felix-1"


class TestEtcdStateStoreGetSet:
    """Tests for get/set operations with mocked etcd3gw client."""

    @pytest.fixture
    def store_with_mock(self):
        store = EtcdStateStore()
        mock_client = MagicMock()
        store._client = mock_client
        return store, mock_client

    @pytest.mark.asyncio
    async def test_get_existing_key(self, store_with_mock):
        store, mock_client = store_with_mock
        data = {"status": "running", "step": 1}
        # etcd3gw get() returns a list of values
        mock_client.get.return_value = [json.dumps(data)]

        result = await store.get("experiments/EXP-001")
        assert result == data

    @pytest.mark.asyncio
    async def test_get_nonexistent_key(self, store_with_mock):
        store, mock_client = store_with_mock
        # etcd3gw returns empty list when key not found
        mock_client.get.return_value = []

        result = await store.get("experiments/NONEXISTENT")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_invalid_json(self, store_with_mock):
        store, mock_client = store_with_mock
        mock_client.get.return_value = ["not json"]

        result = await store.get("experiments/BAD")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_without_ttl(self, store_with_mock):
        store, mock_client = store_with_mock
        data = {"status": "pending"}

        await store.set("experiments/EXP-001", data)

        mock_client.put.assert_called_once_with(
            "/device-connect/state/experiments/EXP-001",
            json.dumps(data),
        )

    @pytest.mark.asyncio
    async def test_set_with_ttl(self, store_with_mock):
        store, mock_client = store_with_mock
        mock_lease = MagicMock()
        mock_client.lease.return_value = mock_lease
        data = {"status": "running"}

        await store.set("experiments/EXP-001", data, ttl=60)

        mock_client.lease.assert_called_once_with(ttl=60)
        # Lease stored under the key (without prefix)
        assert "experiments/EXP-001" in store._leases

    @pytest.mark.asyncio
    async def test_delete_existing(self, store_with_mock):
        store, mock_client = store_with_mock
        mock_client.delete.return_value = True

        result = await store.delete("experiments/EXP-001")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, store_with_mock):
        store, mock_client = store_with_mock
        mock_client.delete.return_value = False

        result = await store.delete("experiments/NONEXISTENT")
        assert result is False


class TestEtcdStateStoreLocking:
    """Tests for distributed lock operations."""

    @pytest.fixture
    def store_with_mock(self):
        store = EtcdStateStore()
        mock_client = MagicMock()
        store._client = mock_client
        return store, mock_client

    @pytest.mark.asyncio
    async def test_acquire_lock_success(self, store_with_mock):
        store, mock_client = store_with_mock
        mock_lease = MagicMock()
        mock_client.lease.return_value = mock_lease
        # etcd3gw create() returns True if key was created
        mock_client.create.return_value = True

        acquired = await store._try_acquire_lock("device_locks/felix-1", ttl=30)
        assert acquired is True
        assert "_lock_device_locks/felix-1" in store._leases

    @pytest.mark.asyncio
    async def test_acquire_lock_contention(self, store_with_mock):
        store, mock_client = store_with_mock
        mock_lease = MagicMock()
        mock_client.lease.return_value = mock_lease
        # etcd3gw create() returns False if key already exists
        mock_client.create.return_value = False

        acquired = await store._try_acquire_lock("device_locks/felix-1", ttl=30)
        assert acquired is False
        assert "_lock_device_locks/felix-1" not in store._leases

    @pytest.mark.asyncio
    async def test_release_lock(self, store_with_mock):
        store, mock_client = store_with_mock
        mock_lease = MagicMock()
        store._leases["_lock_device_locks/felix-1"] = mock_lease

        await store._release_lock("device_locks/felix-1")

        mock_client.delete.assert_called_once_with("/device-connect/locks/device_locks/felix-1")
        assert "_lock_device_locks/felix-1" not in store._leases


class TestEtcdStateStoreGetPrefix:
    """Tests for get_prefix operation."""

    @pytest.fixture
    def store_with_mock(self):
        store = EtcdStateStore()
        mock_client = MagicMock()
        store._client = mock_client
        return store, mock_client

    @pytest.mark.asyncio
    async def test_get_prefix(self, store_with_mock):
        store, mock_client = store_with_mock

        # etcd3gw get_prefix returns list of (value, kv_dict) tuples
        # where kv_dict has base64-encoded keys
        kv1 = {"key": base64.b64encode(b"/device-connect/state/experiments/EXP-001").decode()}
        kv2 = {"key": base64.b64encode(b"/device-connect/state/experiments/EXP-002").decode()}

        mock_client.get_prefix.return_value = [
            (json.dumps({"status": "complete"}), kv1),
            (json.dumps({"status": "running"}), kv2),
        ]

        results = await store.get_prefix("experiments/")

        assert len(results) == 2
        assert results["experiments/EXP-001"]["status"] == "complete"
        assert results["experiments/EXP-002"]["status"] == "running"

    @pytest.mark.asyncio
    async def test_get_prefix_empty(self, store_with_mock):
        store, mock_client = store_with_mock
        mock_client.get_prefix.return_value = []

        results = await store.get_prefix("experiments/")
        assert len(results) == 0


class TestEtcdStateStoreRefreshTtl:
    """Tests for TTL refresh operation."""

    @pytest.fixture
    def store_with_mock(self):
        store = EtcdStateStore()
        mock_client = MagicMock()
        store._client = mock_client
        return store, mock_client

    @pytest.mark.asyncio
    async def test_refresh_existing_lease(self, store_with_mock):
        store, mock_client = store_with_mock
        mock_lease = MagicMock()
        store._leases["experiments/EXP-001"] = mock_lease

        result = await store.refresh_ttl("experiments/EXP-001", ttl=60)
        assert result is True
        mock_lease.refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_refresh_no_lease_key_exists(self, store_with_mock):
        store, mock_client = store_with_mock
        # No lease cached, but key exists in etcd
        data = {"status": "running"}
        mock_client.get.return_value = [json.dumps(data)]

        result = await store.refresh_ttl("experiments/EXP-001", ttl=60)
        assert result is True

    @pytest.mark.asyncio
    async def test_refresh_key_not_found(self, store_with_mock):
        store, mock_client = store_with_mock
        mock_client.get.return_value = []

        result = await store.refresh_ttl("experiments/GONE", ttl=60)
        assert result is False
