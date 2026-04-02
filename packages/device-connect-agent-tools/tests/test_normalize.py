"""Unit tests for device_connect_agent_tools._normalize."""
from device_connect_agent_tools._normalize import (
    _normalize_events,
    _normalize_functions,
    compact_device,
    extract_status,
    full_device,
    fuzzy_filter_by_type,
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
