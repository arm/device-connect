"""Unit tests for registry service RPC handler factories.

Tests the three handler factories in
``device_connect_server.registry.service.main``:

- ``_make_register_handler`` — device registration
- ``_make_list_handler``     — discovery (listDevices, getDevice, describeFleet)
- ``_make_hb_handler``       — heartbeat processing
"""

import asyncio
import json
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from device_connect_server.registry.service.main import (
    _make_register_handler,
    _make_list_handler,
    _make_hb_handler,
    _last_seen,
    _device_ttl,
)
from device_connect_server.security.acl import ACLManager, DeviceACL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TENANT = "test-tenant"
RPC_ID = "req-001"
FIXED_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
FIXED_TS = "2026-04-02T00:00:00Z"
FIXED_TIME = 1743552000.0

VALID_REGISTER_PARAMS = {
    "device_id": "camera-001",
    "device_ttl": 30,
    "capabilities": {
        "description": "Test camera",
        "functions": [{"name": "captureImage", "description": "Capture"}],
        "events": [],
    },
    "identity": {
        "arch": "arm64",
        "host_cpu": "cortex-a72",
        "dram_mb": 4096,
        "device_type": "camera",
    },
    "status": {
        "ts": "2026-04-02T00:00:00Z",
        "location": "lab-A",
    },
}

SAMPLE_DEVICE = {
    "device_id": "camera-001",
    "device_type": "camera",
    "identity": {"device_type": "camera"},
    "status": {"location": "lab-A", "online": True},
    "capabilities": {
        "functions": [{"name": "captureImage", "description": "Capture"}],
        "events": [],
    },
}

SAMPLE_DEVICE_2 = {
    "device_id": "robot-001",
    "device_type": "robot",
    "identity": {"device_type": "robot"},
    "status": {"location": "lab-B", "online": True},
    "capabilities": {
        "functions": [{"name": "moveTo", "description": "Move"}],
        "events": [],
    },
}


def _rpc_request(method: str, params: dict, rpc_id: str = RPC_ID) -> bytes:
    return json.dumps({
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": method,
        "params": params,
    }).encode()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Clear module-level dicts before and after each test."""
    _last_seen.clear()
    _device_ttl.clear()
    yield
    _last_seen.clear()
    _device_ttl.clear()


@pytest.fixture(autouse=True)
def _bypass_to_thread(monkeypatch):
    """Make asyncio.to_thread call the function synchronously."""
    async def _sync(func, *args, **kwargs):
        return func(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", _sync)


@pytest.fixture
def mock_registry():
    """Patch the registry module used by the handler factories."""
    with patch("device_connect_server.registry.service.main.registry") as mock_reg:
        mock_reg.register = MagicMock()
        mock_reg.list_devices = MagicMock(return_value=[])
        mock_reg.get_device = MagicMock(return_value=None)
        mock_reg.refresh = MagicMock()
        mock_reg.update_status = MagicMock()
        yield mock_reg


@pytest.fixture
def messaging():
    """Messaging client with AsyncMock publish."""
    mc = MagicMock()
    mc.publish = AsyncMock()
    return mc


# ---------------------------------------------------------------------------
# TestRegisterHandler
# ---------------------------------------------------------------------------


class TestRegisterHandler:

    @pytest.mark.asyncio
    @patch("device_connect_server.registry.service.main.time")
    @patch("device_connect_server.registry.service.main.uuid")
    async def test_register_success(self, mock_uuid, mock_time, messaging, mock_registry):
        mock_uuid.uuid4.return_value = uuid.UUID(FIXED_UUID)
        mock_time.strftime.return_value = FIXED_TS
        mock_time.time.return_value = FIXED_TIME

        handler = _make_register_handler(TENANT, messaging)
        data = _rpc_request("registerDevice", VALID_REGISTER_PARAMS)
        await handler(data, "reply-subject")

        # Registry was called with correct tenant, device_id, and ttl
        mock_registry.register.assert_called_once()
        call_args = mock_registry.register.call_args
        assert call_args[0][0] == TENANT
        assert call_args[0][1] == "camera-001"
        assert call_args[0][3] == 30  # ttl

        # Payload includes registration metadata
        registry_payload = call_args[0][2]
        assert registry_payload["registry"]["device_registration_id"] == FIXED_UUID
        assert registry_payload["registry"]["registered_at"] == FIXED_TS

    @pytest.mark.asyncio
    @patch("device_connect_server.registry.service.main.time")
    @patch("device_connect_server.registry.service.main.uuid")
    async def test_register_publishes_success_response(self, mock_uuid, mock_time, messaging, mock_registry):
        mock_uuid.uuid4.return_value = uuid.UUID(FIXED_UUID)
        mock_time.strftime.return_value = FIXED_TS
        mock_time.time.return_value = FIXED_TIME

        handler = _make_register_handler(TENANT, messaging)
        await handler(_rpc_request("registerDevice", VALID_REGISTER_PARAMS), "reply-sub")

        # First publish: RPC success response
        first_call = messaging.publish.call_args_list[0]
        assert first_call[0][0] == "reply-sub"
        response = json.loads(first_call[0][1])
        assert response["result"]["status"] == "registered"
        assert response["result"]["device_registration_id"] == FIXED_UUID

    @pytest.mark.asyncio
    @patch("device_connect_server.registry.service.main.time")
    @patch("device_connect_server.registry.service.main.uuid")
    async def test_register_publishes_online_event(self, mock_uuid, mock_time, messaging, mock_registry):
        mock_uuid.uuid4.return_value = uuid.UUID(FIXED_UUID)
        mock_time.strftime.return_value = FIXED_TS
        mock_time.time.return_value = FIXED_TIME

        handler = _make_register_handler(TENANT, messaging)
        await handler(_rpc_request("registerDevice", VALID_REGISTER_PARAMS), "reply-sub")

        # Second publish: device/online event
        assert messaging.publish.call_count == 2
        second_call = messaging.publish.call_args_list[1]
        assert second_call[0][0] == f"device-connect.{TENANT}.device.online"
        event = json.loads(second_call[0][1])
        assert event["method"] == "device/online"
        assert event["params"]["device_id"] == "camera-001"

    @pytest.mark.asyncio
    @patch("device_connect_server.registry.service.main.time")
    @patch("device_connect_server.registry.service.main.uuid")
    async def test_register_updates_module_dicts(self, mock_uuid, mock_time, messaging, mock_registry):
        mock_uuid.uuid4.return_value = uuid.UUID(FIXED_UUID)
        mock_time.strftime.return_value = FIXED_TS
        mock_time.time.return_value = FIXED_TIME

        handler = _make_register_handler(TENANT, messaging)
        await handler(_rpc_request("registerDevice", VALID_REGISTER_PARAMS), "reply-sub")

        key = f"{TENANT}/camera-001"
        assert _device_ttl[key] == 30
        assert _last_seen[key] == FIXED_TIME

    @pytest.mark.asyncio
    async def test_register_wrong_method(self, messaging, mock_registry):
        handler = _make_register_handler(TENANT, messaging)
        data = _rpc_request("wrongMethod", VALID_REGISTER_PARAMS)
        await handler(data, "reply-sub")

        messaging.publish.assert_called_once()
        response = json.loads(messaging.publish.call_args[0][1])
        assert response["error"]["code"] == -32601
        mock_registry.register.assert_not_called()

    @pytest.mark.asyncio
    async def test_register_invalid_params(self, messaging, mock_registry):
        handler = _make_register_handler(TENANT, messaging)
        # Missing required device_id field
        bad_params = {k: v for k, v in VALID_REGISTER_PARAMS.items() if k != "device_id"}
        data = _rpc_request("registerDevice", bad_params)
        await handler(data, "reply-sub")

        response = json.loads(messaging.publish.call_args[0][1])
        assert response["error"]["code"] == -32602
        mock_registry.register.assert_not_called()

    @pytest.mark.asyncio
    async def test_register_bad_ttl(self, messaging, mock_registry):
        handler = _make_register_handler(TENANT, messaging)
        params = {**VALID_REGISTER_PARAMS, "device_ttl": 0}
        data = _rpc_request("registerDevice", params)
        await handler(data, "reply-sub")

        response = json.loads(messaging.publish.call_args[0][1])
        assert response["error"]["code"] == -32602
        mock_registry.register.assert_not_called()

    @pytest.mark.asyncio
    @patch("device_connect_server.registry.service.main.time")
    @patch("device_connect_server.registry.service.main.uuid")
    async def test_register_registry_exception(self, mock_uuid, mock_time, messaging, mock_registry):
        mock_uuid.uuid4.return_value = uuid.UUID(FIXED_UUID)
        mock_time.strftime.return_value = FIXED_TS
        mock_time.time.return_value = FIXED_TIME
        mock_registry.register.side_effect = RuntimeError("etcd timeout")

        handler = _make_register_handler(TENANT, messaging)
        await handler(_rpc_request("registerDevice", VALID_REGISTER_PARAMS), "reply-sub")

        response = json.loads(messaging.publish.call_args[0][1])
        assert response["error"]["code"] == -32603
        assert "etcd timeout" in response["error"]["message"]

    @pytest.mark.asyncio
    async def test_register_malformed_json(self, messaging, mock_registry):
        handler = _make_register_handler(TENANT, messaging)
        await handler(b"not valid json", "reply-sub")

        response = json.loads(messaging.publish.call_args[0][1])
        assert response["error"]["code"] == -32602
        mock_registry.register.assert_not_called()


# ---------------------------------------------------------------------------
# TestListDevicesHandler
# ---------------------------------------------------------------------------


class TestListDevicesHandler:

    @pytest.mark.asyncio
    async def test_list_devices_success(self, messaging, mock_registry):
        mock_registry.list_devices.return_value = [SAMPLE_DEVICE]
        handler = _make_list_handler(TENANT, messaging)
        data = _rpc_request("discovery/listDevices", {})
        await handler(data, "reply-sub")

        mock_registry.list_devices.assert_called_once_with(
            TENANT, device_type=None, location=None,
        )
        response = json.loads(messaging.publish.call_args[0][1])
        assert response["result"]["devices"] == [SAMPLE_DEVICE]

    @pytest.mark.asyncio
    async def test_list_devices_with_filters(self, messaging, mock_registry):
        mock_registry.list_devices.return_value = [SAMPLE_DEVICE]
        handler = _make_list_handler(TENANT, messaging)
        data = _rpc_request("discovery/listDevices", {
            "device_type": "camera", "location": "lab-A",
        })
        await handler(data, "reply-sub")

        mock_registry.list_devices.assert_called_once_with(
            TENANT, device_type="camera", location="lab-A",
        )

    @pytest.mark.asyncio
    async def test_list_devices_empty(self, messaging, mock_registry):
        mock_registry.list_devices.return_value = []
        handler = _make_list_handler(TENANT, messaging)
        await handler(_rpc_request("discovery/listDevices", {}), "reply-sub")

        response = json.loads(messaging.publish.call_args[0][1])
        assert response["result"]["devices"] == []

    @pytest.mark.asyncio
    async def test_list_devices_with_acl(self, messaging, mock_registry):
        mock_registry.list_devices.return_value = [SAMPLE_DEVICE, SAMPLE_DEVICE_2]

        acl_mgr = ACLManager()
        # Hide camera-001 from robot-001
        acl_mgr.set_acl(DeviceACL(
            device_id="camera-001", tenant=TENANT,
            hidden_from=["robot-001"],
        ))

        handler = _make_list_handler(TENANT, messaging, acl_manager=acl_mgr)
        data = _rpc_request("discovery/listDevices", {"requester_id": "robot-001"})
        await handler(data, "reply-sub")

        response = json.loads(messaging.publish.call_args[0][1])
        device_ids = [d["device_id"] for d in response["result"]["devices"]]
        assert "camera-001" not in device_ids
        assert "robot-001" in device_ids

    @pytest.mark.asyncio
    async def test_list_devices_registry_error(self, messaging, mock_registry):
        mock_registry.list_devices.side_effect = RuntimeError("etcd down")
        handler = _make_list_handler(TENANT, messaging)
        await handler(_rpc_request("discovery/listDevices", {}), "reply-sub")

        response = json.loads(messaging.publish.call_args[0][1])
        assert response["error"]["code"] == -32000


# ---------------------------------------------------------------------------
# TestGetDeviceHandler
# ---------------------------------------------------------------------------


class TestGetDeviceHandler:

    @pytest.mark.asyncio
    async def test_get_device_success(self, messaging, mock_registry):
        mock_registry.get_device.return_value = SAMPLE_DEVICE
        handler = _make_list_handler(TENANT, messaging)
        data = _rpc_request("discovery/getDevice", {"device_id": "camera-001"})
        await handler(data, "reply-sub")

        mock_registry.get_device.assert_called_once_with(TENANT, "camera-001")
        response = json.loads(messaging.publish.call_args[0][1])
        assert response["result"]["device"] == SAMPLE_DEVICE

    @pytest.mark.asyncio
    async def test_get_device_not_found(self, messaging, mock_registry):
        mock_registry.get_device.return_value = None
        handler = _make_list_handler(TENANT, messaging)
        data = _rpc_request("discovery/getDevice", {"device_id": "ghost-999"})
        await handler(data, "reply-sub")

        response = json.loads(messaging.publish.call_args[0][1])
        assert response["result"]["device"] is None

    @pytest.mark.asyncio
    async def test_get_device_missing_device_id(self, messaging, mock_registry):
        handler = _make_list_handler(TENANT, messaging)
        data = _rpc_request("discovery/getDevice", {})
        await handler(data, "reply-sub")

        response = json.loads(messaging.publish.call_args[0][1])
        assert response["error"]["code"] == -32602
        assert "device_id required" in response["error"]["message"]
        mock_registry.get_device.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_device_acl_hidden(self, messaging, mock_registry):
        mock_registry.get_device.return_value = SAMPLE_DEVICE

        acl_mgr = ACLManager()
        acl_mgr.set_acl(DeviceACL(
            device_id="camera-001", tenant=TENANT,
            hidden_from=["intruder"],
        ))

        handler = _make_list_handler(TENANT, messaging, acl_manager=acl_mgr)
        data = _rpc_request("discovery/getDevice", {
            "device_id": "camera-001", "requester_id": "intruder",
        })
        await handler(data, "reply-sub")

        response = json.loads(messaging.publish.call_args[0][1])
        assert response["result"]["device"] is None

    @pytest.mark.asyncio
    async def test_get_device_acl_visible(self, messaging, mock_registry):
        mock_registry.get_device.return_value = SAMPLE_DEVICE

        acl_mgr = ACLManager()
        acl_mgr.set_acl(DeviceACL(
            device_id="camera-001", tenant=TENANT,
            visible_to=["*"],
        ))

        handler = _make_list_handler(TENANT, messaging, acl_manager=acl_mgr)
        data = _rpc_request("discovery/getDevice", {
            "device_id": "camera-001", "requester_id": "friend-001",
        })
        await handler(data, "reply-sub")

        response = json.loads(messaging.publish.call_args[0][1])
        assert response["result"]["device"] == SAMPLE_DEVICE

    @pytest.mark.asyncio
    async def test_discovery_malformed_json(self, messaging, mock_registry):
        handler = _make_list_handler(TENANT, messaging)
        await handler(b"not valid json", "reply-sub")

        response = json.loads(messaging.publish.call_args[0][1])
        assert response["error"]["code"] == -32000
        mock_registry.list_devices.assert_not_called()


# ---------------------------------------------------------------------------
# TestDescribeFleetHandler
# ---------------------------------------------------------------------------


class TestDescribeFleetHandler:

    @pytest.mark.asyncio
    async def test_describe_fleet_success(self, messaging, mock_registry):
        mock_registry.list_devices.return_value = [SAMPLE_DEVICE, SAMPLE_DEVICE_2]
        handler = _make_list_handler(TENANT, messaging)
        data = _rpc_request("discovery/describeFleet", {})
        await handler(data, "reply-sub")

        response = json.loads(messaging.publish.call_args[0][1])
        result = response["result"]
        assert result["total_devices"] == 2

    @pytest.mark.asyncio
    async def test_describe_fleet_with_acl(self, messaging, mock_registry):
        mock_registry.list_devices.return_value = [SAMPLE_DEVICE, SAMPLE_DEVICE_2]

        acl_mgr = ACLManager()
        acl_mgr.set_acl(DeviceACL(
            device_id="camera-001", tenant=TENANT,
            hidden_from=["outsider"],
        ))

        handler = _make_list_handler(TENANT, messaging, acl_manager=acl_mgr)
        data = _rpc_request("discovery/describeFleet", {"requester_id": "outsider"})
        await handler(data, "reply-sub")

        response = json.loads(messaging.publish.call_args[0][1])
        # Only robot-001 visible, so total_devices == 1
        assert response["result"]["total_devices"] == 1

    @pytest.mark.asyncio
    async def test_describe_fleet_error(self, messaging, mock_registry):
        mock_registry.list_devices.side_effect = RuntimeError("etcd down")
        handler = _make_list_handler(TENANT, messaging)
        await handler(_rpc_request("discovery/describeFleet", {}), "reply-sub")

        response = json.loads(messaging.publish.call_args[0][1])
        assert response["error"]["code"] == -32000


# ---------------------------------------------------------------------------
# TestDiscoveryUnknownMethod
# ---------------------------------------------------------------------------


class TestDiscoveryUnknownMethod:

    @pytest.mark.asyncio
    async def test_unknown_method_ignored(self, messaging, mock_registry):
        handler = _make_list_handler(TENANT, messaging)
        data = _rpc_request("discovery/unknownMethod", {})
        await handler(data, "reply-sub")

        messaging.publish.assert_not_called()


# ---------------------------------------------------------------------------
# TestHeartbeatHandler
# ---------------------------------------------------------------------------


class TestHeartbeatHandler:

    @pytest.mark.asyncio
    async def test_heartbeat_success(self, mock_registry):
        handler = _make_hb_handler(TENANT)
        data = json.dumps({"device_id": "cam-001", "online": True}).encode()
        await handler(data, None)

        mock_registry.refresh.assert_called_once_with(TENANT, "cam-001")
        mock_registry.update_status.assert_called_once_with(
            TENANT, "cam-001", {"online": True},
        )

    @pytest.mark.asyncio
    async def test_heartbeat_pops_device_id(self, mock_registry):
        handler = _make_hb_handler(TENANT)
        data = json.dumps({"device_id": "cam-001", "battery": 80}).encode()
        await handler(data, None)

        # The data passed to update_status should NOT contain device_id
        status_data = mock_registry.update_status.call_args[0][2]
        assert "device_id" not in status_data
        assert status_data["battery"] == 80

    @pytest.mark.asyncio
    async def test_heartbeat_updates_last_seen(self, mock_registry):
        handler = _make_hb_handler(TENANT)
        before = time.time()
        await handler(json.dumps({"device_id": "cam-001"}).encode(), None)
        after = time.time()

        key = f"{TENANT}/cam-001"
        assert key in _last_seen
        assert before <= _last_seen[key] <= after

    @pytest.mark.asyncio
    async def test_heartbeat_malformed_json(self, mock_registry):
        handler = _make_hb_handler(TENANT)
        # Should not raise — exception is logged internally
        await handler(b"not json", None)
        mock_registry.refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_heartbeat_missing_device_id(self, mock_registry):
        handler = _make_hb_handler(TENANT)
        await handler(json.dumps({"online": True}).encode(), None)
        mock_registry.refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_heartbeat_registry_exception(self, mock_registry):
        mock_registry.refresh.side_effect = RuntimeError("etcd down")
        handler = _make_hb_handler(TENANT)
        # Should not raise
        await handler(json.dumps({"device_id": "cam-001"}).encode(), None)
