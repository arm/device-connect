"""Unit tests for device_connect_agent_tools.tools module.

Tests discover_devices, invoke_device, invoke_device_with_fallback,
and get_device_status using a mocked connection (no real NATS).
"""

from unittest.mock import MagicMock, patch

import pytest

from device_connect_agent_tools import tools as tools_mod


# ── Fixtures ──────────────────────────────────────────────────────

# Mock data matches the flattened format returned by _flatten_device
# (device_type, location, functions, events all at top level).
SAMPLE_DEVICES = [
    {
        "device_id": "cam-001",
        "device_type": "camera",
        "location": "lab-A",
        "status": {"state": "online"},
        "identity": {"device_type": "camera"},
        "capabilities": {
            "functions": [
                {"name": "capture_image", "description": "Capture an image", "parameters": {"type": "object"}},
            ],
            "events": [
                {"name": "state_change_detected"},
                {"name": "object_detected"},
            ],
        },
        "functions": [
            {"name": "capture_image", "description": "Capture an image", "parameters": {"type": "object"}},
        ],
        "events": [
            {"name": "state_change_detected"},
            {"name": "object_detected"},
        ],
    },
    {
        "device_id": "robot-001",
        "device_type": "cleaning_robot",
        "location": "lab-A",
        "status": {"state": "idle"},
        "identity": {"device_type": "cleaning_robot"},
        "capabilities": {
            "functions": [
                {"name": "dispatch_robot", "description": "Dispatch robot", "parameters": {}},
            ],
            "events": [
                {"name": "cleaning_finished"},
            ],
        },
        "functions": [
            {"name": "dispatch_robot", "description": "Dispatch robot", "parameters": {}},
        ],
        "events": [
            {"name": "cleaning_finished"},
        ],
    },
    {
        "device_id": "sensor-001",
        "device_type": "environment_sensor",
        "location": "lab-B",
        "status": {"state": "online"},
        "identity": {"device_type": "environment_sensor"},
        "capabilities": {
            "functions": [
                {"name": "get_reading", "description": "Get sensor reading", "parameters": {}},
            ],
            "events": [],
        },
        "functions": [
            {"name": "get_reading", "description": "Get sensor reading", "parameters": {}},
        ],
        "events": [],
    },
]


@pytest.fixture
def mock_conn():
    """Provide a mock _DeviceConnectConnection connection and patch get_connection."""
    conn = MagicMock()
    conn.list_devices.return_value = SAMPLE_DEVICES
    with patch.object(tools_mod, "get_connection", return_value=conn):
        yield conn


# ── discover_devices ──────────────────────────────────────────────


class TestDiscoverDevices:
    def test_discover_all(self, mock_conn):
        devices = tools_mod.discover_devices()
        assert len(devices) == 3
        assert devices[0]["device_id"] == "cam-001"
        assert devices[1]["device_id"] == "robot-001"

    def test_discover_by_type(self, mock_conn):
        devices = tools_mod.discover_devices(device_type="camera")
        assert len(devices) == 1
        assert devices[0]["device_id"] == "cam-001"

    def test_discover_fuzzy_type(self, mock_conn):
        # "robot" should match "cleaning_robot" via fuzzy matching
        devices = tools_mod.discover_devices(device_type="robot")
        assert len(devices) == 1
        assert devices[0]["device_id"] == "robot-001"

    def test_discover_returns_functions(self, mock_conn):
        devices = tools_mod.discover_devices()
        cam = devices[0]
        assert len(cam["functions"]) == 1
        assert cam["functions"][0]["name"] == "capture_image"

    def test_discover_returns_events(self, mock_conn):
        devices = tools_mod.discover_devices()
        cam = devices[0]
        assert "state_change_detected" in cam["events"]
        assert "object_detected" in cam["events"]

    def test_discover_connection_error(self, mock_conn):
        mock_conn.list_devices.side_effect = Exception("Connection lost")
        devices = tools_mod.discover_devices()
        assert devices == []

    def test_discover_no_match_returns_empty(self, mock_conn):
        # If fuzzy filter matches nothing, returns empty list
        mock_conn.list_devices.return_value = SAMPLE_DEVICES
        devices = tools_mod.discover_devices(device_type="nonexistent_xyz")
        assert devices == []

    def test_discover_handles_flat_functions(self, mock_conn):
        """When functions are at top level (not nested in capabilities)."""
        mock_conn.list_devices.return_value = [{
            "device_id": "dev-1",
            "device_type": "test",
            "functions": [{"name": "ping", "description": "Ping"}],
            "events": ["alert"],
        }]
        devices = tools_mod.discover_devices()
        assert devices[0]["functions"][0]["name"] == "ping"
        assert devices[0]["events"] == ["alert"]


# ── invoke_device ─────────────────────────────────────────────────


class TestInvokeDevice:
    def test_invoke_success(self, mock_conn):
        mock_conn.invoke.return_value = {
            "jsonrpc": "2.0", "id": "1", "result": {"temperature": 25.0},
        }
        result = tools_mod.invoke_device("sensor-001", "get_reading")
        assert result["success"] is True
        assert result["result"]["temperature"] == 25.0

    def test_invoke_with_params(self, mock_conn):
        mock_conn.invoke.return_value = {"jsonrpc": "2.0", "id": "1", "result": {"status": "dispatched"}}
        result = tools_mod.invoke_device(
            "robot-001", "dispatch_robot",
            params={"zone_id": "zone-A"},
        )
        assert result["success"] is True
        # Verify params were passed through
        mock_conn.invoke.assert_called_once_with(
            "robot-001", "dispatch_robot", params={"zone_id": "zone-A"},
        )

    def test_invoke_strips_llm_reasoning_from_params(self, mock_conn):
        mock_conn.invoke.return_value = {"jsonrpc": "2.0", "id": "1", "result": {}}
        tools_mod.invoke_device(
            "robot-001", "dispatch_robot",
            params={"zone_id": "zone-A", "llm_reasoning": "should be stripped"},
            llm_reasoning="Camera detected spill",
        )
        call_params = mock_conn.invoke.call_args[1]["params"]
        assert "llm_reasoning" not in call_params
        assert call_params["zone_id"] == "zone-A"

    def test_invoke_rpc_error(self, mock_conn):
        mock_conn.invoke.return_value = {
            "jsonrpc": "2.0", "id": "1",
            "error": {"code": -32601, "message": "Method not found"},
        }
        result = tools_mod.invoke_device("cam-001", "unknown_func")
        assert result["success"] is False
        assert "Method not found" in result["error"]

    def test_invoke_connection_error(self, mock_conn):
        mock_conn.invoke.side_effect = Exception("Timeout")
        result = tools_mod.invoke_device("cam-001", "capture_image")
        assert result["success"] is False
        assert "Timeout" in result["error"]

    def test_invoke_no_params(self, mock_conn):
        mock_conn.invoke.return_value = {"jsonrpc": "2.0", "id": "1", "result": {"ok": True}}
        result = tools_mod.invoke_device("cam-001", "ping")
        assert result["success"] is True
        mock_conn.invoke.assert_called_once_with("cam-001", "ping", params={})


# ── invoke_device_with_fallback ───────────────────────────────────


class TestInvokeDeviceWithFallback:
    def test_first_device_succeeds(self, mock_conn):
        mock_conn.invoke.return_value = {"jsonrpc": "2.0", "id": "1", "result": {"ok": True}}
        result = tools_mod.invoke_device_with_fallback(
            ["robot-001", "robot-002"], "dispatch_robot",
        )
        assert result["success"] is True
        assert result["device_id"] == "robot-001"
        assert mock_conn.invoke.call_count == 1

    def test_fallback_to_second(self, mock_conn):
        mock_conn.invoke.side_effect = [
            Exception("robot-001 offline"),
            {"jsonrpc": "2.0", "id": "2", "result": {"ok": True}},
        ]
        result = tools_mod.invoke_device_with_fallback(
            ["robot-001", "robot-002"], "dispatch_robot",
        )
        assert result["success"] is True
        assert result["device_id"] == "robot-002"

    def test_all_fail(self, mock_conn):
        mock_conn.invoke.side_effect = [
            Exception("offline"),
            Exception("offline"),
        ]
        result = tools_mod.invoke_device_with_fallback(
            ["robot-001", "robot-002"], "dispatch_robot",
        )
        assert result["success"] is False
        assert "All devices failed" in result["error"]
        assert len(result["failed_devices"]) == 2

    def test_fallback_on_rpc_error(self, mock_conn):
        mock_conn.invoke.side_effect = [
            {"error": {"code": -32601, "message": "busy"}},
            {"jsonrpc": "2.0", "id": "2", "result": {"dispatched": True}},
        ]
        result = tools_mod.invoke_device_with_fallback(
            ["robot-001", "robot-002"], "dispatch_robot",
        )
        assert result["success"] is True
        assert result["device_id"] == "robot-002"


# ── get_device_status ─────────────────────────────────────────────


class TestGetDeviceStatus:
    def test_device_found(self, mock_conn):
        mock_conn.get_device.return_value = {
            "device_id": "cam-001",
            "device_type": "camera",
            "location": "lab-A",
            "status": {"state": "online"},
            "functions": [{"name": "capture_image"}],
        }
        result = tools_mod.get_device_status("cam-001")
        assert result["device_id"] == "cam-001"
        assert result["device_type"] == "camera"
        assert "capture_image" in result["functions"]

    def test_device_not_found(self, mock_conn):
        mock_conn.get_device.return_value = None
        result = tools_mod.get_device_status("nonexistent")
        assert "error" in result
        assert "not found" in result["error"]

    def test_connection_error(self, mock_conn):
        mock_conn.get_device.side_effect = Exception("NATS down")
        result = tools_mod.get_device_status("cam-001")
        assert "error" in result
        assert "NATS down" in result["error"]
