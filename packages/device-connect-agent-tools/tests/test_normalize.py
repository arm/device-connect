"""Unit tests for device_connect_agent_tools._normalize."""
from device_connect_agent_tools._normalize import (
    _normalize_events,
    _normalize_functions,
    aggregate_fleet,
    compact_device,
    extract_status,
    full_device,
    fuzzy_filter_by_type,
    group_devices,
)


# ── _normalize_functions ───────────────────────────────────────


class TestNormalizeFunctions:
    def test_dict_with_all_fields(self):
        funcs = [{"name": "read", "description": "Read sensor", "parameters": {"type": "object"}}]
        result = _normalize_functions(funcs)
        assert result == [{"name": "read", "description": "Read sensor", "parameters": {"type": "object"}}]

    def test_dict_missing_optional_fields(self):
        funcs = [{"name": "stop"}]
        result = _normalize_functions(funcs)
        assert result == [{"name": "stop", "description": "", "parameters": {}}]

    def test_plain_string(self):
        funcs = ["get_reading"]
        result = _normalize_functions(funcs)
        assert result == [{"name": "get_reading", "description": "", "parameters": {}}]

    def test_filters_none_name(self):
        funcs = [{"name": None}, {"name": "ok"}]
        result = _normalize_functions(funcs)
        assert len(result) == 1
        assert result[0]["name"] == "ok"

    def test_filters_empty_name(self):
        funcs = [{"name": ""}, {"name": "ok"}]
        result = _normalize_functions(funcs)
        assert len(result) == 1

    def test_empty_list(self):
        assert _normalize_functions([]) == []

    def test_mixed_dicts_and_strings(self):
        funcs = [{"name": "a", "description": "desc"}, "b"]
        result = _normalize_functions(funcs)
        assert len(result) == 2
        assert result[0]["description"] == "desc"
        assert result[1]["description"] == ""


# ── _normalize_events ──────────────────────────────────────────


class TestNormalizeEvents:
    def test_dict_events(self):
        events = [{"name": "temperature"}, {"name": "humidity"}]
        assert _normalize_events(events) == ["temperature", "humidity"]

    def test_string_events(self):
        events = ["alert", "motion"]
        assert _normalize_events(events) == ["alert", "motion"]

    def test_mixed(self):
        events = [{"name": "a"}, "b"]
        assert _normalize_events(events) == ["a", "b"]

    def test_filters_none_name(self):
        events = [{"name": None}, "ok"]
        assert _normalize_events(events) == ["ok"]

    def test_empty_list(self):
        assert _normalize_events([]) == []


# ── fuzzy_filter_by_type ───────────────────────────────────────


class TestFuzzyFilterByType:
    DEVICES = [
        {"device_id": "cam-1", "device_type": "camera"},
        {"device_id": "robot-1", "device_type": "cleaning_robot"},
        {"device_id": "sensor-1", "device_type": "environment-sensor"},
    ]

    def test_exact_match(self):
        result = fuzzy_filter_by_type(self.DEVICES, "camera")
        assert len(result) == 1
        assert result[0]["device_id"] == "cam-1"

    def test_case_insensitive(self):
        result = fuzzy_filter_by_type(self.DEVICES, "Camera")
        assert len(result) == 1

    def test_ignores_underscores(self):
        result = fuzzy_filter_by_type(self.DEVICES, "cleaning_robot")
        assert len(result) == 1
        assert result[0]["device_id"] == "robot-1"

    def test_ignores_hyphens(self):
        result = fuzzy_filter_by_type(self.DEVICES, "environment-sensor")
        assert len(result) == 1
        assert result[0]["device_id"] == "sensor-1"

    def test_partial_match(self):
        result = fuzzy_filter_by_type(self.DEVICES, "robot")
        assert len(result) == 1
        assert result[0]["device_id"] == "robot-1"

    def test_no_match(self):
        result = fuzzy_filter_by_type(self.DEVICES, "microphone")
        assert len(result) == 0

    def test_reverse_direction_not_matched(self):
        """Searching for 'camera_sensor' should NOT match a device of type 'camera'."""
        result = fuzzy_filter_by_type(self.DEVICES, "camera_sensor_robot")
        assert len(result) == 0

    def test_empty_devices(self):
        assert fuzzy_filter_by_type([], "camera") == []

    def test_device_without_type(self):
        devices = [{"device_id": "x"}]
        assert fuzzy_filter_by_type(devices, "camera") == []


# ── full_device ────────────────────────────────────────────────


class TestFullDevice:
    def test_basic(self):
        d = {
            "device_id": "cam-1",
            "device_type": "camera",
            "location": "lobby",
            "functions": [{"name": "snap", "description": "Take photo", "parameters": {}}],
            "events": [{"name": "motion"}],
        }
        result = full_device(d)
        assert result["device_id"] == "cam-1"
        assert result["device_type"] == "camera"
        assert result["location"] == "lobby"
        assert len(result["functions"]) == 1
        assert result["functions"][0]["name"] == "snap"
        assert result["events"] == ["motion"]

    def test_missing_fields_default(self):
        result = full_device({})
        assert result["device_id"] is None
        assert result["functions"] == []
        assert result["events"] == []


# ── compact_device ─────────────────────────────────────────────


class TestCompactDevice:
    DEVICE = {
        "device_id": "cam-1",
        "device_type": "camera",
        "location": "lobby",
        "functions": [
            {"name": "snap", "description": "Take photo", "parameters": {"type": "object"}},
            {"name": "zoom", "description": "Zoom lens", "parameters": {}},
        ],
        "events": ["motion"],
    }

    def test_compact_without_expand(self):
        result = compact_device(self.DEVICE)
        assert result["device_id"] == "cam-1"
        assert result["function_count"] == 2
        assert result["function_names"] == ["snap", "zoom"]
        assert "functions" not in result

    def test_compact_with_expand(self):
        result = compact_device(self.DEVICE, expand=True)
        assert result["function_count"] == 2
        assert "functions" in result
        assert len(result["functions"]) == 2
        assert result["functions"][0]["name"] == "snap"

    def test_empty_functions(self):
        result = compact_device({"device_id": "x"})
        assert result["function_count"] == 0
        assert result["function_names"] == []


class TestExtractStatus:

    def test_availability(self):
        d = {"status": {"availability": "idle"}}
        assert extract_status(d) == "idle"

    def test_state_fallback(self):
        d = {"status": {"state": "online"}}
        assert extract_status(d) == "online"

    def test_availability_takes_precedence(self):
        d = {"status": {"availability": "busy", "state": "online"}}
        assert extract_status(d) == "busy"

    def test_missing_status(self):
        assert extract_status({}) == "unknown"

    def test_non_dict_status(self):
        assert extract_status({"status": "online"}) == "unknown"

    def test_custom_default(self):
        assert extract_status({}, default="n/a") == "n/a"

    def test_empty_status_dict(self):
        assert extract_status({"status": {}}) == "unknown"


# ── aggregate_fleet ───────────────────────────────────────────


class TestAggregateFleet:

    def test_empty_list(self):
        result = aggregate_fleet([])
        assert result["total_devices"] == 0
        assert result["total_functions"] == 0
        assert result["by_type"] == {}
        assert result["by_location"] == {}

    def test_single_device(self):
        devices = [{"device_type": "camera", "location": "lobby", "functions": [{"name": "snap"}]}]
        result = aggregate_fleet(devices)
        assert result["total_devices"] == 1
        assert result["total_functions"] == 1
        assert result["by_type"] == {"camera": {"count": 1, "locations": ["lobby"]}}
        assert result["by_location"] == {"lobby": {"count": 1, "types": ["camera"]}}

    def test_multiple_devices(self):
        devices = [
            {"device_type": "camera", "location": "lobby", "functions": [{"name": "snap"}]},
            {"device_type": "camera", "location": "lab", "functions": [{"name": "snap"}, {"name": "zoom"}]},
            {"device_type": "robot", "location": "lab", "functions": [{"name": "move"}]},
        ]
        result = aggregate_fleet(devices)
        assert result["total_devices"] == 3
        assert result["total_functions"] == 4
        assert result["by_type"]["camera"]["count"] == 2
        assert sorted(result["by_type"]["camera"]["locations"]) == ["lab", "lobby"]
        assert result["by_type"]["robot"]["count"] == 1
        assert result["by_location"]["lab"]["count"] == 2
        assert sorted(result["by_location"]["lab"]["types"]) == ["camera", "robot"]

    def test_none_type_becomes_unknown(self):
        devices = [{"device_type": None, "location": "lobby", "functions": []}]
        result = aggregate_fleet(devices)
        assert "unknown" in result["by_type"]

    def test_none_location_becomes_unknown(self):
        devices = [{"device_type": "camera", "location": None, "functions": []}]
        result = aggregate_fleet(devices)
        assert "unknown" in result["by_location"]

    def test_missing_type_and_location(self):
        devices = [{"functions": []}]
        result = aggregate_fleet(devices)
        assert "unknown" in result["by_type"]
        assert "unknown" in result["by_location"]

    def test_function_counting(self):
        devices = [
            {"device_type": "a", "location": "x", "functions": ["f1", "f2"]},
            {"device_type": "b", "location": "y", "functions": ["f3"]},
        ]
        result = aggregate_fleet(devices)
        assert result["total_functions"] == 3

    def test_keys_are_sorted(self):
        devices = [
            {"device_type": "zebra", "location": "zoo", "functions": []},
            {"device_type": "ant", "location": "farm", "functions": []},
        ]
        result = aggregate_fleet(devices)
        assert list(result["by_type"].keys()) == ["ant", "zebra"]
        assert list(result["by_location"].keys()) == ["farm", "zoo"]


# ── group_devices ─────────────────────────────────────────────


class TestGroupDevices:

    DEVICES = [
        {"device_id": "cam-1", "device_type": "camera", "location": "lobby",
         "functions": [{"name": "snap"}], "status": {"availability": "idle"}},
        {"device_id": "cam-2", "device_type": "camera", "location": "lab",
         "functions": [{"name": "snap"}], "status": {"availability": "busy"}},
        {"device_id": "robot-1", "device_type": "robot", "location": "lab",
         "functions": [{"name": "move"}], "status": {"state": "online"}},
    ]

    def test_group_by_type(self):
        result = group_devices(self.DEVICES, "device_type", expand=False)
        assert result["total"] == 3
        assert "camera" in result["groups"]
        assert "robot" in result["groups"]
        assert len(result["groups"]["camera"]) == 2
        assert len(result["groups"]["robot"]) == 1

    def test_group_by_location(self):
        result = group_devices(self.DEVICES, "location", expand=False)
        assert "lobby" in result["groups"]
        assert "lab" in result["groups"]
        assert len(result["groups"]["lab"]) == 2

    def test_none_field_becomes_unknown(self):
        devices = [{"device_id": "x", "device_type": None, "functions": []}]
        result = group_devices(devices, "device_type", expand=False)
        assert "unknown" in result["groups"]

    def test_expand_true_includes_functions(self):
        result = group_devices(self.DEVICES, "device_type", expand=True)
        cam = result["groups"]["camera"][0]
        assert "functions" in cam

    def test_expand_false_excludes_functions(self):
        result = group_devices(self.DEVICES, "device_type", expand=False)
        cam = result["groups"]["camera"][0]
        assert "functions" not in cam
        assert "function_count" in cam

    def test_empty_list(self):
        result = group_devices([], "device_type", expand=False)
        assert result["total"] == 0
        assert result["groups"] == {}

    def test_each_entry_has_status(self):
        result = group_devices(self.DEVICES, "device_type", expand=False)
        for group_entries in result["groups"].values():
            for entry in group_entries:
                assert "status" in entry

    def test_groups_are_sorted(self):
        result = group_devices(self.DEVICES, "device_type", expand=False)
        assert list(result["groups"].keys()) == ["camera", "robot"]
