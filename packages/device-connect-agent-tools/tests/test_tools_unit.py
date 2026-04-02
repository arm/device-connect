"""Unit tests for device_connect_agent_tools.tools module.

Tests discover_devices, invoke_device, invoke_device_with_fallback,
and get_device_status using a mocked connection (no real NATS).
"""

from unittest.mock import MagicMock, patch

import pytest

from device_connect_agent_tools import tools as tools_mod


# ── Fixtures ──────────────────────────────────────────────────────

# Mock data matches the flattened format returned by flatten_device
# (device_type, location, functions, events all at top level).
SAMPLE_DEVICES = [
    {
        "device_id": "cam-001",
        "device_type": "camera",
        "location": "lab-A",
        "status": {"state": "online"},
        "identity": {"device_type": "camera"},
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
        "functions": [
            {"name": "get_reading", "description": "Get sensor reading", "parameters": {}},
        ],
        "events": [],
    },
]


@pytest.fixture
def mock_conn():
    """Provide a mock DeviceConnection and patch get_connection."""
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


# ── describe_fleet ───────────────────────────────────────────────


class TestDescribeFleet:
    def test_fleet_summary(self, mock_conn):
        result = tools_mod.describe_fleet()
        assert result["total_devices"] == 3
        assert result["total_functions"] == 3
        assert "camera" in result["by_type"]
        assert result["by_type"]["camera"]["count"] == 1
        assert "lab-A" in result["by_type"]["camera"]["locations"]
        assert "lab-A" in result["by_location"]
        assert result["by_location"]["lab-A"]["count"] == 2  # cam + robot

    def test_fleet_summary_empty(self, mock_conn):
        mock_conn.list_devices.return_value = []
        result = tools_mod.describe_fleet()
        assert result["total_devices"] == 0
        assert result["total_functions"] == 0
        assert result["by_type"] == {}

    def test_fleet_summary_connection_error(self, mock_conn):
        mock_conn.list_devices.side_effect = Exception("down")
        result = tools_mod.describe_fleet()
        assert result["total_devices"] == 0

    def test_fleet_auto_includes_devices_small(self, mock_conn):
        """Small fleet (3 devices, threshold=5) → full device details included."""
        result = tools_mod.describe_fleet()
        assert "devices" in result
        assert "hint" in result
        assert len(result["devices"]) == 3
        # Each device should have full function schemas
        cam = next(d for d in result["devices"] if d["device_id"] == "cam-001")
        assert len(cam["functions"]) == 1
        assert cam["functions"][0]["name"] == "capture_image"
        assert "parameters" in cam["functions"][0]

    @patch.object(tools_mod, "SMALL_FLEET_THRESHOLD", 1)
    def test_fleet_auto_excludes_devices_large(self, mock_conn):
        """Fleet above threshold → no devices key."""
        result = tools_mod.describe_fleet()
        assert result["total_devices"] == 3
        assert "devices" not in result
        assert "hint" not in result

    @patch.object(tools_mod, "SMALL_FLEET_THRESHOLD", 0)
    def test_fleet_threshold_zero_disables(self, mock_conn):
        """Threshold=0 disables auto-expansion even for small fleets."""
        result = tools_mod.describe_fleet()
        assert result["total_devices"] == 3
        assert "devices" not in result


# ── list_devices (new hierarchical) ─────────────────────────────


class TestListDevices:
    def test_list_all(self, mock_conn):
        result = tools_mod.list_devices()
        assert result["total"] == 3
        assert len(result["devices"]) == 3
        # Compact fields always present
        for d in result["devices"]:
            assert "function_count" in d
            assert "function_names" in d

    def test_list_with_type_filter(self, mock_conn):
        result = tools_mod.list_devices(device_type="camera")
        assert result["total"] == 1
        assert result["devices"][0]["device_id"] == "cam-001"

    def test_list_with_pagination(self, mock_conn):
        result = tools_mod.list_devices(offset=0, limit=2)
        assert result["total"] == 3
        assert len(result["devices"]) == 2
        assert result["has_more"] is True

        result2 = tools_mod.list_devices(offset=2, limit=2)
        assert len(result2["devices"]) == 1
        assert result2["has_more"] is False

    def test_list_group_by_location(self, mock_conn):
        result = tools_mod.list_devices(group_by="location")
        assert "groups" in result
        assert "lab-A" in result["groups"]
        assert "lab-B" in result["groups"]
        assert len(result["groups"]["lab-A"]) == 2  # cam + robot
        assert len(result["groups"]["lab-B"]) == 1  # sensor

    def test_list_group_by_device_type(self, mock_conn):
        result = tools_mod.list_devices(group_by="device_type")
        assert "groups" in result
        assert "camera" in result["groups"]
        assert "cleaning_robot" in result["groups"]

    def test_list_no_match(self, mock_conn):
        result = tools_mod.list_devices(device_type="nonexistent")
        assert result["total"] == 0
        assert result["devices"] == []

    def test_list_connection_error(self, mock_conn):
        mock_conn.list_devices.side_effect = Exception("down")
        result = tools_mod.list_devices()
        assert result["total"] == 0

    def test_list_auto_includes_functions_small(self, mock_conn):
        """Small result set (3 devices, threshold=5) → schemas included."""
        result = tools_mod.list_devices()
        for d in result["devices"]:
            assert "functions" in d
            assert "parameters" in str(d["functions"])

    @patch.object(tools_mod, "SMALL_FLEET_THRESHOLD", 1)
    def test_list_auto_excludes_functions_large(self, mock_conn):
        """Result set above threshold → no functions key in devices."""
        result = tools_mod.list_devices()
        for d in result["devices"]:
            assert "functions" not in d

    @patch.object(tools_mod, "SMALL_FLEET_THRESHOLD", 1)
    def test_list_grouped_no_auto_expand(self, mock_conn):
        """Grouped results above threshold → no functions key."""
        result = tools_mod.list_devices(group_by="location")
        for devices_in_group in result["groups"].values():
            for d in devices_in_group:
                assert "functions" not in d

    def test_list_with_location_filter(self, mock_conn):
        """Filter by location passes through to provider."""
        tools_mod.list_devices(location="lab-B")
        # location is passed to conn.list_devices; mock returns all,
        # so we verify the call was made with location kwarg
        mock_conn.list_devices.assert_called_with(location="lab-B")

    def test_list_with_status_filter(self, mock_conn):
        """Filter by status uses extract_status client-side."""
        result = tools_mod.list_devices(status="online")
        # cam-001 (online) and sensor-001 (online) match; robot-001 (idle) does not
        assert result["total"] == 2
        ids = [d["device_id"] for d in result["devices"]]
        assert "cam-001" in ids
        assert "sensor-001" in ids
        assert "robot-001" not in ids

    def test_list_with_type_and_location_filter(self, mock_conn):
        """Combined type and location filtering."""
        result = tools_mod.list_devices(device_type="camera", location="lab-A")
        assert result["total"] == 1
        assert result["devices"][0]["device_id"] == "cam-001"


# ── get_device_functions ─────────────────────────────────────────


class TestGetDeviceFunctions:
    def test_get_functions(self, mock_conn):
        mock_conn.get_device.return_value = SAMPLE_DEVICES[0]
        result = tools_mod.get_device_functions("cam-001")
        assert result["device_id"] == "cam-001"
        assert result["device_type"] == "camera"
        assert len(result["functions"]) == 1
        assert result["functions"][0]["name"] == "capture_image"
        assert "parameters" in result["functions"][0]
        assert len(result["events"]) == 2

    def test_device_not_found(self, mock_conn):
        mock_conn.get_device.return_value = None
        result = tools_mod.get_device_functions("nonexistent")
        assert "error" in result
        assert "not found" in result["error"]

    def test_connection_error(self, mock_conn):
        mock_conn.get_device.side_effect = Exception("timeout")
        result = tools_mod.get_device_functions("cam-001")
        assert "error" in result
