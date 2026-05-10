# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the selector-driven ``discover`` and ``discover_labels`` tools.

Uses a labeled mock fleet (cam-001, robot-001, sensor-001) drawn from the
existing DC test driver vocabulary so every selector exercises real device,
function, and event names.
"""
from unittest.mock import MagicMock, patch

import pytest

from device_connect_agent_tools import tools as tools_mod


# -- Fixture: labeled fleet ---------------------------------------


SAMPLE_DEVICES = [
    {
        "device_id": "cam-001",
        "device_type": "camera",
        "location": "lab-A",
        "status": {"state": "online"},
        "identity": {"device_type": "camera"},
        "labels": {"category": ["camera", "inference"], "location": "zone-A/dock"},
        "functions": [
            {
                "name": "capture_image",
                "description": "Capture an image",
                "parameters": {"type": "object"},
                "labels": {"direction": "write", "modality": "rgb"},
            },
        ],
        "events": [
            {"name": "object_detected", "labels": {"modality": "rgb"}},
            {"name": "state_change_detected", "labels": None},
        ],
    },
    {
        "device_id": "cam-002",
        "device_type": "camera",
        "location": "lab-A",
        "status": {"state": "online"},
        "identity": {"device_type": "camera"},
        "labels": {"category": "camera", "location": "zone-B/dock"},
        "functions": [
            {
                "name": "capture_image",
                "description": "Capture an image",
                "parameters": {"type": "object"},
                "labels": {"direction": "write", "modality": ["rgb", "4k"]},
            },
        ],
        "events": [
            {"name": "object_detected", "labels": {"modality": "rgb"}},
        ],
    },
    {
        "device_id": "robot-001",
        "device_type": "cleaner_robot",
        "location": "lab-A",
        "status": {"state": "idle"},
        "identity": {"device_type": "cleaner_robot"},
        "labels": {"category": "robot", "location": "zone-A/yard"},
        "functions": [
            {
                "name": "dispatch_robot",
                "description": "Dispatch",
                "parameters": {},
                "labels": {"direction": "write", "safety": "critical"},
            },
            {
                "name": "get_status",
                "description": "Status",
                "parameters": {},
                "labels": {"direction": "read"},
            },
        ],
        "events": [
            {"name": "cleaning_finished", "labels": None},
        ],
    },
    {
        "device_id": "sensor-001",
        "device_type": "temperature_sensor",
        "location": "lab-B",
        "status": {"state": "online"},
        "identity": {"device_type": "temperature_sensor"},
        "labels": {"category": "sensor", "location": "lab-B"},
        "functions": [
            {"name": "get_reading", "parameters": {}, "labels": {"direction": "read"}},
            {"name": "set_threshold", "parameters": {}, "labels": {"direction": "write"}},
            {"name": "set_location", "parameters": {}, "labels": {"direction": "write"}},
        ],
        "events": [
            {"name": "reading", "labels": None},
            {"name": "threshold_exceeded", "labels": {"safety": "informational"}},
        ],
    },
]


@pytest.fixture
def mock_conn():
    conn = MagicMock()
    conn.list_devices.return_value = SAMPLE_DEVICES
    with patch.object(tools_mod, "get_connection", return_value=conn):
        yield conn


# -- discover: device-only scope -----------------------------------


class TestDiscoverDeviceOnly:
    def test_match_by_category_label(self, mock_conn):
        r = tools_mod.discover("device(category:camera)")
        assert r["scope"] == "device_only"
        assert r["matched"] == 2
        assert {row["device_id"] for row in r["results"]} == {"cam-001", "cam-002"}

    def test_multivalued_match_picks_composite_only(self, mock_conn):
        # Only cam-001 has category:[camera, inference].
        r = tools_mod.discover("device(category:inference)")
        assert r["matched"] == 1
        assert r["results"][0]["device_id"] == "cam-001"

    def test_or_within_key(self, mock_conn):
        r = tools_mod.discover("device(category:[camera,robot])")
        assert {row["device_id"] for row in r["results"]} == {
            "cam-001", "cam-002", "robot-001"
        }

    def test_glob_location(self, mock_conn):
        r = tools_mod.discover("device(location:zone-A/*)")
        assert {row["device_id"] for row in r["results"]} == {"cam-001", "robot-001"}

    def test_and_across_keys(self, mock_conn):
        r = tools_mod.discover(
            "device(category:[camera,robot], location:zone-A/*)"
        )
        assert {row["device_id"] for row in r["results"]} == {"cam-001", "robot-001"}

    def test_match_all(self, mock_conn):
        r = tools_mod.discover("device(*)")
        assert r["matched"] == 4

    def test_bare_id_match(self, mock_conn):
        r = tools_mod.discover("device(cam-001)")
        assert r["matched"] == 1
        assert r["results"][0]["device_id"] == "cam-001"

    def test_labels_surfaced_in_result(self, mock_conn):
        r = tools_mod.discover("device(cam-001)")
        assert r["results"][0]["labels"] == {
            "category": ["camera", "inference"],
            "location": "zone-A/dock",
        }


# -- discover: function scope --------------------------------------


class TestDiscoverFunctionScope:
    def test_writes_fleet_wide(self, mock_conn):
        r = tools_mod.discover("device(*).function(direction:write)")
        assert r["scope"] == "device_function"
        assert r["matched"] == 5  # capture x2, dispatch_robot, set_threshold, set_location
        names = {row["name"] for row in r["results"]}
        assert names == {"capture_image", "dispatch_robot", "set_threshold", "set_location"}

    def test_function_only_scope_by_name(self, mock_conn):
        r = tools_mod.discover("function(get_reading)")
        assert r["scope"] == "function_only"
        assert r["matched"] == 1
        assert r["results"][0]["name"] == "get_reading"
        assert r["results"][0]["device_id"] == "sensor-001"

    def test_anchored_glob_set_prefix(self, mock_conn):
        r = tools_mod.discover("function(set_*)")
        assert {row["name"] for row in r["results"]} == {"set_threshold", "set_location"}

    def test_below_threshold_returns_full_schemas(self, mock_conn):
        r = tools_mod.discover("device(cam-001).function(*)")
        assert r["matched"] == 1
        row = r["results"][0]
        assert "parameters" in row
        assert "description" in row
        assert row["labels"] == {"direction": "write", "modality": "rgb"}

    def test_modality_or_within_key(self, mock_conn):
        r = tools_mod.discover("device(*).function(modality:[rgb,thermal])")
        assert r["matched"] == 2
        assert all(row["name"] == "capture_image" for row in r["results"])

    def test_safety_critical_filter(self, mock_conn):
        r = tools_mod.discover("function(safety:critical)")
        assert r["matched"] == 1
        assert r["results"][0]["name"] == "dispatch_robot"

    def test_label_histogram_built(self, mock_conn):
        r = tools_mod.discover("device(*).function(direction:write)")
        hist = r["label_histogram"]
        assert hist["direction"]["values"] == {"write": 5}
        # modality is multi-valued on cam-002 (rgb + 4k)
        modality = hist["modality"]
        assert modality.get("multivalued") is True
        assert modality["values"] == {"rgb": 2, "4k": 1}


# -- discover: event scope -----------------------------------------


class TestDiscoverEventScope:
    def test_event_by_modality(self, mock_conn):
        r = tools_mod.discover("device(*).event(modality:rgb)")
        assert r["scope"] == "device_event"
        assert r["matched"] == 2  # cam-001 + cam-002 each emit object_detected
        assert all(row["name"] == "object_detected" for row in r["results"])

    def test_event_only_by_name(self, mock_conn):
        r = tools_mod.discover("event(threshold_exceeded)")
        assert r["scope"] == "event_only"
        assert r["matched"] == 1


# -- discover: pagination ------------------------------------------


class TestDiscoverPagination:
    def test_pagination_envelope(self, mock_conn):
        r = tools_mod.discover("device(*)", limit=2)
        assert r["matched"] == 4
        assert r["returned"] == 2
        assert r["offset"] == 0
        assert r["next_offset"] == 2

    def test_offset_respected(self, mock_conn):
        r = tools_mod.discover("device(*)", offset=2, limit=10)
        assert r["offset"] == 2
        assert r["returned"] == 2
        assert r["next_offset"] is None

    def test_negative_offset_clamped(self, mock_conn):
        r = tools_mod.discover("device(*)", offset=-5)
        assert r["offset"] == 0

    def test_hard_limit_caps_runaway_request(self, mock_conn):
        r = tools_mod.discover("device(*)", limit=999_999)
        # Hard ceiling is 1000; for 4 devices, the page just returns everything.
        assert r["returned"] == 4

    def test_zero_limit_falls_back_to_default(self, mock_conn):
        r = tools_mod.discover("device(*)", limit=0)
        # Default applies, all 4 fit in one page.
        assert r["returned"] == 4


# -- discover: errors ----------------------------------------------


class TestDiscoverErrors:
    def test_bad_selector_returns_error_envelope(self, mock_conn):
        r = tools_mod.discover("not a selector at all")
        assert "error" in r
        assert r["matched"] == 0
        assert r["results"] == []

    def test_unknown_scope_in_selector(self, mock_conn):
        r = tools_mod.discover("widgets(*)")
        assert "error" in r
        assert "unknown scope" in r["error"].lower()

    def test_connection_failure_returns_error(self):
        broken = MagicMock()
        broken.list_devices.side_effect = RuntimeError("messaging down")
        with patch.object(tools_mod, "get_connection", return_value=broken):
            r = tools_mod.discover("device(*)")
        assert "error" in r
        assert r["matched"] == 0

    def test_non_string_selector(self, mock_conn):
        r = tools_mod.discover(None)  # type: ignore[arg-type]
        assert "error" in r


# -- discover_labels ------------------------------------------------


class TestDiscoverLabels:
    def test_multi_axis_default(self, mock_conn):
        v = tools_mod.discover_labels()
        assert v["total_devices"] == 4
        assert v["total_functions"] == 7
        assert v["total_events"] == 6
        assert "category" in v["device_keys"]
        assert "direction" in v["function_keys"]
        assert "modality" in v["event_keys"]

    def test_multivalued_annotation_on_device_category(self, mock_conn):
        v = tools_mod.discover_labels()
        cat = v["device_keys"]["category"]
        assert cat["multivalued"] is True
        # All 4 devices declared a category; cam-001 contributed to two values
        # but unique_devices counts distinct devices.
        assert cat["unique_devices"] == 4
        assert cat["values"] == {"camera": 2, "inference": 1, "robot": 1, "sensor": 1}

    def test_singleton_keys_not_flagged_multivalued(self, mock_conn):
        v = tools_mod.discover_labels()
        direction = v["function_keys"]["direction"]
        assert direction.get("multivalued") is not True

    def test_per_key_pagination(self, mock_conn):
        v = tools_mod.discover_labels(key="device.location")
        assert v["axis"] == "device"
        assert v["key"] == "location"
        # 4 distinct location values, sorted by frequency desc then alpha
        assert v["matched"] == 4
        assert list(v["values"].keys())[0] == "lab-B"  # only single value with count 1, alpha tiebreak

    def test_per_key_function_axis(self, mock_conn):
        v = tools_mod.discover_labels(key="function.direction")
        assert v["axis"] == "function"
        assert v["values"] == {"write": 5, "read": 2}

    def test_per_key_unknown_axis(self, mock_conn):
        v = tools_mod.discover_labels(key="thing.bogus")
        assert "error" in v

    def test_per_key_missing_dot(self, mock_conn):
        v = tools_mod.discover_labels(key="just_a_key")
        assert "error" in v
        assert "axis-qualified" in v["error"]


# -- Deprecation warnings ------------------------------------------


class TestDeprecationWarnings:
    def test_describe_fleet_emits_warning(self, mock_conn, recwarn):
        tools_mod.describe_fleet()
        assert any("describe_fleet" in str(w.message) for w in recwarn.list)

    def test_list_devices_emits_warning(self, mock_conn, recwarn):
        tools_mod.list_devices()
        assert any("list_devices" in str(w.message) for w in recwarn.list)

    def test_get_device_functions_emits_warning(self, mock_conn, recwarn):
        # get_device_functions calls conn.get_device which we haven't mocked;
        # the warning is emitted before that call so we still observe it.
        mock_conn.get_device = MagicMock(return_value={
            "device_id": "cam-001", "functions": [], "events": [],
            "identity": {}, "status": {}, "capabilities": {},
        })
        # Force a fresh patch so get_device path is hit
        with patch.object(tools_mod, "get_connection", return_value=mock_conn):
            tools_mod.get_device_functions("cam-001")
        assert any("get_device_functions" in str(w.message) for w in recwarn.list)
