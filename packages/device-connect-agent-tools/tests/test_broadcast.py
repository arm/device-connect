# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the selector-driven ``broadcast`` tool.

Uses the same labeled mock fleet (cam-001, cam-002, robot-001, sensor-001)
as the discover/invoke tests so selectors exercise real device, function,
and event names.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from device_connect_agent_tools import tools as tools_mod


SAMPLE_DEVICES = [
    {
        "device_id": "cam-001",
        "device_type": "camera",
        "location": "lab-A",
        "status": {"state": "online"},
        "identity": {"device_type": "camera"},
        "labels": {"category": "camera", "location": "lab-A"},
        "functions": [
            {
                "name": "capture_image",
                "parameters": {},
                "labels": {"direction": "write", "modality": "rgb"},
            },
        ],
        "events": [],
    },
    {
        "device_id": "cam-002",
        "device_type": "camera",
        "location": "lab-A",
        "status": {"state": "online"},
        "identity": {"device_type": "camera"},
        "labels": {"category": "camera", "location": "lab-A"},
        "functions": [
            {
                "name": "capture_image",
                "parameters": {},
                "labels": {"direction": "write", "modality": "rgb"},
            },
        ],
        "events": [],
    },
    {
        "device_id": "sensor-001",
        "device_type": "temperature_sensor",
        "location": "lab-B",
        "status": {"state": "online"},
        "identity": {"device_type": "temperature_sensor"},
        "labels": {"category": "sensor"},
        "functions": [
            {
                "name": "get_reading",
                "parameters": {},
                "labels": {"direction": "read"},
            },
        ],
        "events": [],
    },
]


@pytest.fixture
def mock_conn():
    conn = MagicMock()
    conn.list_devices.return_value = SAMPLE_DEVICES
    conn.zone = "default"
    # Capture the published envelope for assertions.
    published: list[dict] = []
    conn.publish_broadcast.side_effect = lambda env: published.append(env)
    conn._published = published
    with patch.object(tools_mod, "get_connection", return_value=conn):
        yield conn


# -- broadcast ------------------------------------------------------


class TestBroadcast:
    def test_returns_correlation_id_and_candidates(self, mock_conn):
        r = tools_mod.broadcast("device(*).function(capture_image)")
        assert r["correlation_id"].startswith("br-")
        assert r["candidates"] == 2
        assert r["function"] == "capture_image"
        assert "error" not in r

    def test_envelope_carries_function_and_targets(self, mock_conn):
        tools_mod.broadcast(
            "device(*).function(capture_image)",
            params={"resolution": "4k"},
        )
        env = mock_conn._published[0]
        assert env["function"] == "capture_image"
        assert env["params"] == {"resolution": "4k"}
        assert sorted(env["targets"]) == ["cam-001", "cam-002"]
        # No optional fields when caller did not set them.
        assert "where" not in env
        assert "bindings" not in env
        assert "fire_at" not in env
        assert "on_late" not in env

    def test_where_and_bindings_propagate_to_envelope(self, mock_conn):
        tools_mod.broadcast(
            "device(*).function(capture_image)",
            where="status.battery > 50",
            bindings={"threshold": 80},
        )
        env = mock_conn._published[0]
        assert env["where"] == "status.battery > 50"
        assert env["bindings"] == {"threshold": 80}

    def test_fire_at_propagates_with_default_on_late(self, mock_conn):
        tools_mod.broadcast(
            "device(*).function(capture_image)",
            fire_at=123456789.0,
        )
        env = mock_conn._published[0]
        assert env["fire_at"] == 123456789.0
        assert env["on_late"] == "skip"

    def test_fire_at_with_explicit_on_late_fire(self, mock_conn):
        tools_mod.broadcast(
            "device(*).function(capture_image)",
            fire_at=123.0, on_late="fire",
        )
        env = mock_conn._published[0]
        assert env["on_late"] == "fire"

    def test_invalid_on_late_rejected(self, mock_conn):
        r = tools_mod.broadcast(
            "device(*).function(capture_image)", on_late="bogus",
        )
        assert r["candidates"] == 0
        assert r["error"]["code"] == "invalid_on_late"
        assert mock_conn.publish_broadcast.call_count == 0

    def test_ambiguous_function_rejected(self, mock_conn):
        # function(direction:read) resolves to multiple distinct functions
        # (get_reading + dispatch_robot's get_status if it had read; here
        # it just hits sensor's get_reading and possibly more). With our
        # SAMPLE_DEVICES this matches just get_reading, so artificially
        # broaden by picking a selector that crosses functions:
        r = tools_mod.broadcast("device(*).function(*)")
        assert r["candidates"] == 3
        assert r["error"]["code"] == "ambiguous_function"

    def test_zero_matches_returns_correlation_with_zero(self, mock_conn):
        r = tools_mod.broadcast("device(*).function(does_not_exist)")
        assert r["candidates"] == 0
        assert r["correlation_id"].startswith("br-")
        # No envelope was published (no targets).
        assert mock_conn.publish_broadcast.call_count == 0

    def test_invalid_scope_rejected(self, mock_conn):
        r = tools_mod.broadcast("device(cam-001)")
        assert r["candidates"] == 0
        assert r["error"]["code"] == "invalid_invoke_scope"

    def test_selector_parse_error_propagated(self, mock_conn):
        r = tools_mod.broadcast("widgets(*)")
        assert r["candidates"] == 0
        assert r["error"]["code"] == "selector_parse_error"

    def test_invalid_predicate_rejected_before_publish(self, mock_conn):
        # The predicate is compile-validated at the dispatcher; a syntax
        # error short-circuits without publishing.
        try:
            import celpy  # noqa: F401
        except ImportError:
            pytest.skip("cel-python not installed")
        r = tools_mod.broadcast(
            "device(*).function(capture_image)", where="a > > b",
        )
        assert r["error"]["code"] == "invalid_predicate"
        assert mock_conn.publish_broadcast.call_count == 0

    def test_publish_failure_returns_connection_error(self):
        conn = MagicMock()
        conn.list_devices.return_value = SAMPLE_DEVICES
        conn.zone = "default"
        conn.publish_broadcast.side_effect = RuntimeError("messaging down")
        with patch.object(tools_mod, "get_connection", return_value=conn):
            r = tools_mod.broadcast("device(*).function(capture_image)")
        assert r["error"]["code"] == "connection_error"
        assert "messaging down" in r["error"]["message"]

    def test_llm_reasoning_stripped_from_params(self, mock_conn):
        tools_mod.broadcast(
            "device(*).function(capture_image)",
            params={"resolution": "4k", "llm_reasoning": "should not appear"},
        )
        env = mock_conn._published[0]
        assert "llm_reasoning" not in env["params"]
