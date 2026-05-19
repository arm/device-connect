# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the selector DSL parser and matcher.

Parses selector strings like
``device(category:camera, location:warehouse1/*).function(direction:write)``
into a structured Selector and matches it against label dicts.
"""
import pytest

from device_connect_edge.selector import (
    Filter,
    KeyFilter,
    Scope,
    Selector,
    SelectorParseError,
    parse_selector,
)


# -- KeyFilter -----------------------------------------------------


class TestKeyFilter:
    def test_single_value_str_label(self):
        kf = KeyFilter("direction", ("write",))
        assert kf.matches("write")
        assert not kf.matches("read")

    def test_none_label_never_matches(self):
        assert not KeyFilter("direction", ("write",)).matches(None)

    def test_list_label_any_member_matches(self):
        kf = KeyFilter("category", ("camera",))
        assert kf.matches(["camera", "inference"])
        assert not kf.matches(["robot", "inference"])

    def test_or_within_key(self):
        kf = KeyFilter("category", ("camera", "robot"))
        assert kf.matches("camera")
        assert kf.matches("robot")
        assert not kf.matches("hub")
        assert kf.matches(["camera", "inference"])

    def test_glob_value(self):
        kf = KeyFilter("location", ("warehouse1/*",))
        assert kf.matches("warehouse1/loading-dock")
        assert kf.matches("warehouse1/yard")
        assert not kf.matches("warehouse2/dock")

    def test_subtree_glob_matches_exact_and_descendants(self):
        # ``lab-A*`` matches both the exact location and any descendants.
        kf = KeyFilter("location", ("lab-A*",))
        assert kf.matches("lab-A")
        assert kf.matches("lab-A/optics-bench")
        assert not kf.matches("lab-B")

    def test_character_class_glob(self):
        # ``[abc]rgb`` is an fnmatch character class — should match argb/brgb/crgb
        # and not literal text "[abc]rgb". Regression guard against treating
        # ``[``-bearing patterns as literal strings.
        kf = KeyFilter("modality", ("[abc]rgb",))
        assert kf.matches("argb")
        assert kf.matches("brgb")
        assert kf.matches("crgb")
        assert not kf.matches("drgb")
        assert not kf.matches("[abc]rgb")

    def test_negated_character_class_glob(self):
        # ``[!abc]rgb`` matches any single character not in {a,b,c} followed by ``rgb``.
        kf = KeyFilter("modality", ("[!abc]rgb",))
        assert kf.matches("drgb")
        assert kf.matches("xrgb")
        assert not kf.matches("argb")


# -- Filter --------------------------------------------------------


class TestFilter:
    def test_empty_filter_matches_anything(self):
        f = Filter()
        assert f.matches("anything", None)
        assert f.matches("foo", {"k": "v"})

    def test_name_match_exact(self):
        f = Filter(name_match="robot-001")
        assert f.matches("robot-001", None)
        assert not f.matches("robot-002", None)

    def test_name_match_glob(self):
        f = Filter(name_match="set_*")
        assert f.matches("set_threshold", {})
        assert f.matches("set_location", {})
        assert not f.matches("get_reading", {})

    def test_name_match_character_class_glob(self):
        # Brackets-only pattern (no ``*`` / ``?``) exercises the glob-detection
        # heuristic in isolation — a ``*`` in the pattern would route through
        # fnmatch regardless and hide the bug.
        f = Filter(name_match="[sg]et_threshold")
        assert f.matches("set_threshold", {})
        assert f.matches("get_threshold", {})
        assert not f.matches("put_threshold", {})
        assert not f.matches("[sg]et_threshold", {})

    def test_and_across_keys(self):
        f = Filter(
            key_filters=(
                KeyFilter("category", ("camera",)),
                KeyFilter("location", ("warehouse1/*",)),
            )
        )
        assert f.matches("cam1", {"category": "camera", "location": "warehouse1/dock"})
        assert not f.matches("cam1", {"category": "camera", "location": "warehouse2/dock"})
        assert not f.matches("cam1", {"category": "robot", "location": "warehouse1/dock"})

    def test_name_and_label_combined(self):
        f = Filter(
            name_match="set_*",
            key_filters=(KeyFilter("direction", ("write",)),),
        )
        assert f.matches("set_threshold", {"direction": "write"})
        assert not f.matches("set_threshold", {"direction": "read"})
        assert not f.matches("get_reading", {"direction": "write"})

    def test_missing_label_means_no_match(self):
        f = Filter(key_filters=(KeyFilter("safety", ("critical",)),))
        assert not f.matches("foo", {})
        assert not f.matches("foo", None)


# -- Selector vacuous axes -----------------------------------------


class TestSelectorVacuous:
    """Unset axes return True so callers can iterate without scope branching."""

    def test_device_only_function_vacuous(self):
        s = Selector(scope=Scope.DEVICE_ONLY, device=Filter())
        assert s.matches_function("anything", {"direction": "write"})
        assert s.matches_event("anything", None)

    def test_function_only_device_vacuous(self):
        s = Selector(scope=Scope.FUNCTION_ONLY, function=Filter())
        assert s.matches_device("any-id", None)


# -- parse_selector: scope shapes ---------------------------------


class TestParseScope:
    def test_device_only(self):
        s = parse_selector("device(category:camera)")
        assert s.scope == Scope.DEVICE_ONLY
        assert s.device == Filter(key_filters=(KeyFilter("category", ("camera",)),))
        assert s.function is None
        assert s.event is None

    def test_function_only(self):
        s = parse_selector("function(safety:critical)")
        assert s.scope == Scope.FUNCTION_ONLY
        assert s.function.key_filters == (KeyFilter("safety", ("critical",)),)

    def test_event_only(self):
        s = parse_selector("event(modality:motion)")
        assert s.scope == Scope.EVENT_ONLY
        assert s.event.key_filters == (KeyFilter("modality", ("motion",)),)

    def test_device_function(self):
        s = parse_selector("device(*).function(direction:write)")
        assert s.scope == Scope.DEVICE_FUNCTION
        assert s.device == Filter()
        assert s.function.key_filters == (KeyFilter("direction", ("write",)),)

    def test_device_event(self):
        s = parse_selector("device(*).event(modality:motion)")
        assert s.scope == Scope.DEVICE_EVENT

    def test_bare_id_match(self):
        s = parse_selector("device(robot-001)")
        assert s.device.name_match == "robot-001"

    def test_function_name_match(self):
        s = parse_selector("function(estop)")
        assert s.function.name_match == "estop"

    def test_wildcard_matches_anything(self):
        s = parse_selector("device(*)")
        assert s.device == Filter()

    def test_raw_preserved(self):
        sel = "device(category:camera)"
        assert parse_selector(sel).raw == sel

    def test_whitespace_tolerated(self):
        s = parse_selector(
            "  device( category : camera ) . function( direction : write )  "
        )
        assert s.scope == Scope.DEVICE_FUNCTION
        assert s.device.key_filters == (KeyFilter("category", ("camera",)),)
        assert s.function.key_filters == (KeyFilter("direction", ("write",)),)


# -- parse_selector: filter body grammar ---------------------------


class TestParseFilterBody:
    def test_or_within_key(self):
        s = parse_selector("device(category:[camera,robot])")
        assert s.device.key_filters == (KeyFilter("category", ("camera", "robot")),)

    def test_and_across_keys(self):
        s = parse_selector("device(category:camera, location:warehouse1/*)")
        assert s.device.key_filters == (
            KeyFilter("category", ("camera",)),
            KeyFilter("location", ("warehouse1/*",)),
        )

    def test_combined_or_and_glob(self):
        s = parse_selector("device(category:[camera,robot], location:warehouse1/*)")
        assert s.device.key_filters == (
            KeyFilter("category", ("camera", "robot")),
            KeyFilter("location", ("warehouse1/*",)),
        )

    def test_bare_name_plus_keys(self):
        s = parse_selector("device(temperature_sensor).function(direction:write, set_*)")
        assert s.device.name_match == "temperature_sensor"
        assert s.function.name_match == "set_*"
        assert s.function.key_filters == (KeyFilter("direction", ("write",)),)


# -- parse_selector: errors ----------------------------------------


class TestParseErrors:
    @pytest.mark.parametrize("bad,expected", [
        ("", "empty"),
        ("   ", "empty"),
        ("device", "expected '('"),
        ("device(", "unclosed"),
        ("foo(x)", "unknown scope"),
        ("function(*).device(*)", "must start with"),
        ("device(*).device(*)", "expected 'function' or 'event'"),
        ("device(*).function(*).event(*)", "unexpected trailing"),
        ("device(*) extra", "unexpected character"),
        ("device(robot-001, robot-002)", "multiple bare-name"),
        ("device(key:)", "empty value"),
        ("device(:value)", "invalid key"),
        ("device(,)", "empty term"),
        ("device(key:[)", "unclosed '['"),
        ("device(key:[])", "empty value list"),
        ("device(key:[a,])", "empty value in list"),
        ("device(key:[[a]])", "nested"),
        ("device(bad key:val)", "invalid key"),
    ])
    def test_error_messages(self, bad, expected):
        with pytest.raises(SelectorParseError) as exc:
            parse_selector(bad)
        assert expected.lower() in str(exc.value).lower()

    def test_non_string_input(self):
        with pytest.raises(SelectorParseError):
            parse_selector(123)  # type: ignore[arg-type]

    def test_error_includes_position_caret(self):
        with pytest.raises(SelectorParseError) as exc:
            parse_selector("device(foo, bad key:v)")
        msg = str(exc.value)
        assert "device(foo, bad key:v)" in msg
        assert "^" in msg


# -- Worked examples -----------------------------------------------


class TestWorkedExamples:
    """End-to-end parse + match using DC-native device kinds (camera, robot,
    sensor) and the labels that drivers would carry."""

    def test_all_cameras(self):
        s = parse_selector("device(category:camera)")
        assert s.matches_device("cam-001", {"category": "camera"})
        # composite identity: camera that also runs inference
        assert s.matches_device("cam-002", {"category": ["camera", "inference"]})
        assert not s.matches_device("robot-001", {"category": "robot"})

    def test_or_within_key_with_zone_filter(self):
        # cameras or robots in zone-A
        s = parse_selector("device(category:[camera,robot], location:zone-A/*)")
        assert s.matches_device(
            "cam-1", {"category": "camera", "location": "zone-A/loading-dock"}
        )
        assert s.matches_device(
            "robot-1", {"category": "robot", "location": "zone-A/yard"}
        )
        assert not s.matches_device(
            "hub-1", {"category": "hub", "location": "zone-A/dock"}
        )
        assert not s.matches_device(
            "cam-2", {"category": "camera", "location": "zone-B/dock"}
        )

    def test_zone_subtree(self):
        # ``zone-A*`` glob matches both ``zone-A`` exactly and any descendant.
        s = parse_selector("device(location:zone-A*)")
        assert s.matches_device("d", {"location": "zone-A"})
        assert s.matches_device("d", {"location": "zone-A/dock"})
        assert not s.matches_device("d", {"location": "zone-B"})

    def test_capture_writes_fleet_wide(self):
        # ``capture_image`` is DC's canonical camera RPC. Filtering for write
        # direction + rgb modality across the fleet picks it up.
        s = parse_selector("device(*).function(direction:write, modality:rgb)")
        assert s.scope == Scope.DEVICE_FUNCTION
        assert s.matches_device("anything", None)
        assert s.matches_function(
            "capture_image", {"direction": "write", "modality": "rgb"}
        )
        assert s.matches_function(
            "capture_image", {"direction": "write", "modality": ["rgb", "4k"]}
        )
        assert not s.matches_function(
            "get_status", {"direction": "read", "modality": "rgb"}
        )
        assert not s.matches_function(
            "capture_image", {"direction": "write", "modality": "thermal"}
        )

    def test_object_detection_events_fleet_wide(self):
        # The ``test_camera`` driver emits ``object_detected`` events; subscribe
        # to it across the fleet via a bare-name event match.
        s = parse_selector("device(*).event(object_detected)")
        assert s.scope == Scope.DEVICE_EVENT
        assert s.matches_event("object_detected", None)
        assert not s.matches_event("state_change_detected", None)

    def test_critical_rpcs_fleetwide(self):
        s = parse_selector("function(safety:critical)")
        assert s.matches_function("estop", {"safety": "critical"})
        assert not s.matches_function("get_reading", {"safety": "informational"})

    def test_estop_name_match_ignores_labels(self):
        # Fleet-wide ESTOP target by reserved name, regardless of labels.
        s = parse_selector("function(estop)")
        assert s.matches_function("estop", None)
        assert s.matches_function("estop", {"safety": "critical"})
        assert not s.matches_function("get_reading", {"safety": "critical"})

    def test_chained_sensor_writes_with_name_glob(self):
        # The ``temperature_sensor`` driver exposes ``set_threshold`` and
        # ``set_location`` (writes) plus ``get_reading`` (read). The anchored
        # glob ``set_*`` selects only the writers.
        s = parse_selector(
            "device(temperature_sensor).function(direction:write, set_*)"
        )
        assert s.matches_device("temperature_sensor", None)
        assert not s.matches_device("test_camera", None)
        assert s.matches_function("set_threshold", {"direction": "write"})
        assert s.matches_function("set_location", {"direction": "write"})
        # Anchored glob: a function whose name does NOT start with ``set_``
        # never matches, regardless of direction.
        assert not s.matches_function("get_reading", {"direction": "read"})
        # Right name shape, wrong direction -> rejected.
        assert not s.matches_function("set_threshold", {"direction": "read"})

    def test_substring_glob_finds_reading_in_either_direction(self):
        # Anchored globs are the default; for substring intent callers wrap
        # with ``*...*``. ``*reading*`` finds the sensor's getter and the event.
        s = parse_selector("function(*reading*)")
        assert s.matches_function("get_reading", {"direction": "read"})
        assert s.matches_function("readings_summary", None)
        assert not s.matches_function("set_threshold", {"direction": "write"})
