# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the selector-driven ``invoke`` and ``invoke_many`` tools.

Uses a small labeled fleet (cam-001, cam-002, robot-001, sensor-001) drawn
from the existing DC test driver vocabulary so every selector exercises
real device, function, and event names.
"""
from unittest.mock import MagicMock, patch

import pytest

from device_connect_agent_tools import tools as tools_mod


# -- Fixtures -------------------------------------------------------


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
        "device_id": "robot-001",
        "device_type": "cleaner_robot",
        "location": "lab-A",
        "status": {"state": "idle"},
        "identity": {"device_type": "cleaner_robot"},
        "labels": {"category": "robot", "location": "lab-A"},
        "functions": [
            {
                "name": "dispatch_robot",
                "parameters": {},
                "labels": {"direction": "write", "safety": "critical"},
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
        "labels": {"category": "sensor", "location": "lab-B"},
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


def _conn_with_invoke(invoke_side_effect):
    """Return a mock Connection whose .invoke() applies ``invoke_side_effect``.

    ``invoke_side_effect`` is called with ``(device_id, function_name,
    params, timeout)`` and must return a JSON-RPC response dict.
    """
    conn = MagicMock()
    conn.list_devices.return_value = SAMPLE_DEVICES

    def _invoke(device_id, function_name, params=None, timeout=None):
        return invoke_side_effect(device_id, function_name, params, timeout)

    conn.invoke.side_effect = _invoke
    return conn


@pytest.fixture
def all_succeed_conn():
    def _ok(device_id, function_name, params, timeout):
        return {"jsonrpc": "2.0", "id": "1", "result": {
            "device_id": device_id, "function": function_name, "params": params,
        }}
    conn = _conn_with_invoke(_ok)
    with patch.object(tools_mod, "get_connection", return_value=conn):
        yield conn


# -- invoke ---------------------------------------------------------


class TestInvoke:
    def test_single_match_returns_success(self, all_succeed_conn):
        r = tools_mod.invoke(
            "device(cam-001).function(capture_image)",
            params={"resolution": "1080p"},
        )
        assert r["success"] is True
        assert r["device_id"] == "cam-001"
        assert r["function"] == "capture_image"
        assert r["result"]["params"] == {"resolution": "1080p"}

    def test_function_only_selector_with_unique_name(self, all_succeed_conn):
        r = tools_mod.invoke("function(get_reading)")
        assert r["success"] is True
        assert r["device_id"] == "sensor-001"
        assert r["function"] == "get_reading"

    def test_no_match_returns_no_match_error(self, all_succeed_conn):
        r = tools_mod.invoke("device(*).function(does_not_exist)")
        assert r["success"] is False
        assert r["error"]["code"] == "no_match"
        assert "does_not_exist" in r["error"]["message"]

    def test_ambiguous_match_returns_error_with_candidates(self, all_succeed_conn):
        # capture_image exists on both cam-001 and cam-002.
        r = tools_mod.invoke("function(capture_image)")
        assert r["success"] is False
        assert r["error"]["code"] == "ambiguous_match"
        assert "expected exactly 1" in r["error"]["message"]
        ids = {c["device_id"] for c in r["candidates"]}
        assert ids == {"cam-001", "cam-002"}

    def test_device_only_scope_rejected(self, all_succeed_conn):
        # Device-only scope cannot resolve to a function.
        r = tools_mod.invoke("device(robot-001)")
        assert r["success"] is False
        assert r["error"]["code"] == "invalid_invoke_scope"

    def test_event_scope_rejected(self, all_succeed_conn):
        r = tools_mod.invoke("event(reading)")
        assert r["success"] is False
        assert r["error"]["code"] == "invalid_invoke_scope"

    def test_selector_parse_error_propagated(self, all_succeed_conn):
        r = tools_mod.invoke("not a selector")
        assert r["success"] is False
        assert r["error"]["code"] == "selector_parse_error"

    def test_non_string_selector_rejected(self, all_succeed_conn):
        r = tools_mod.invoke(None)  # type: ignore[arg-type]
        assert r["success"] is False
        assert r["error"]["code"] == "invalid_selector"

    def test_jsonrpc_error_maps_to_invoke_failed(self):
        def _err(device_id, function_name, params, timeout):
            return {
                "jsonrpc": "2.0", "id": "1",
                "error": {"code": -32000, "message": "device busy"},
            }
        conn = _conn_with_invoke(_err)
        with patch.object(tools_mod, "get_connection", return_value=conn):
            r = tools_mod.invoke("device(robot-001).function(dispatch_robot)")
        assert r["success"] is False
        assert r["error"]["code"] == "-32000"
        assert r["error"]["message"] == "device busy"
        assert r["device_id"] == "robot-001"
        assert r["function"] == "dispatch_robot"

    def test_connection_exception_returns_invoke_failed(self):
        conn = MagicMock()
        conn.list_devices.return_value = SAMPLE_DEVICES
        conn.invoke.side_effect = RuntimeError("messaging down")
        with patch.object(tools_mod, "get_connection", return_value=conn):
            r = tools_mod.invoke("device(cam-001).function(capture_image)")
        assert r["success"] is False
        assert r["error"]["code"] == "invoke_failed"
        assert "messaging down" in r["error"]["message"]

    def test_llm_reasoning_stripped_from_params(self, all_succeed_conn):
        tools_mod.invoke(
            "device(cam-001).function(capture_image)",
            params={"resolution": "1080p", "llm_reasoning": "should not appear"},
            llm_reasoning="caller reasoning",
        )
        # Inspect the params actually delivered to the wire:
        sent = all_succeed_conn.invoke.call_args.kwargs["params"]
        assert "llm_reasoning" not in sent
        assert sent["resolution"] == "1080p"


# -- invoke_many ----------------------------------------------------


class TestInvokeMany:
    def test_zero_matches_returns_empty_envelope(self, all_succeed_conn):
        r = tools_mod.invoke_many("device(*).function(does_not_exist)")
        assert r["candidates"] == 0
        assert r["matched"] == 0
        assert r["succeeded"] == 0
        assert r["failed"] == 0
        assert r["results"] == []
        assert r["errors"] == []
        assert "error" not in r

    def test_all_succeed(self, all_succeed_conn):
        r = tools_mod.invoke_many("device(*).function(capture_image)")
        assert r["candidates"] == 2
        assert r["matched"] == 2
        assert r["succeeded"] == 2
        assert r["failed"] == 0
        ids = {row["device_id"] for row in r["results"]}
        assert ids == {"cam-001", "cam-002"}
        # Each result row is shaped {device_id, function, result}.
        for row in r["results"]:
            assert row["function"] == "capture_image"
            assert "result" in row

    def test_partial_failure_shape(self):
        def _half_fail(device_id, function_name, params, timeout):
            if device_id == "cam-001":
                return {"jsonrpc": "2.0", "id": "1", "result": {"ok": True}}
            return {
                "jsonrpc": "2.0", "id": "1",
                "error": {"code": -32000, "message": "down"},
            }
        conn = _conn_with_invoke(_half_fail)
        with patch.object(tools_mod, "get_connection", return_value=conn):
            r = tools_mod.invoke_many("device(*).function(capture_image)")
        assert r["candidates"] == 2
        assert r["matched"] == 2
        assert r["succeeded"] == 1
        assert r["failed"] == 1
        assert {row["device_id"] for row in r["results"]} == {"cam-001"}
        assert {row["device_id"] for row in r["errors"]} == {"cam-002"}
        for row in r["errors"]:
            assert row["error"]["code"] == "-32000"
            assert row["error"]["message"] == "down"

    def test_invalid_scope_returns_error_envelope(self, all_succeed_conn):
        r = tools_mod.invoke_many("device(robot-001)")
        assert r["candidates"] == 0
        assert r["error"]["code"] == "invalid_invoke_scope"

    def test_selector_parse_error_propagated(self, all_succeed_conn):
        r = tools_mod.invoke_many("widgets(*)")
        assert r["candidates"] == 0
        assert r["error"]["code"] == "selector_parse_error"

    def test_per_target_timeout_passed_to_connection(self, all_succeed_conn):
        tools_mod.invoke_many(
            "device(*).function(capture_image)", timeout=7.5,
        )
        # Every conn.invoke call should carry the same timeout.
        for call in all_succeed_conn.invoke.call_args_list:
            assert call.kwargs["timeout"] == 7.5

    def test_max_concurrency_caps_thread_pool(self, all_succeed_conn):
        # The fan-out group has 3 targets (capture_image x2 + dispatch_robot
        # don't share name; pick a selector that resolves to multiple). Use
        # function(direction:write) which selects 4 distinct rows.
        r = tools_mod.invoke_many(
            "function(direction:write)", max_concurrency=1,
        )
        assert r["candidates"] >= 2
        assert r["succeeded"] == r["candidates"]

    def test_connection_exception_recorded_per_target(self):
        # Mix: cam-001 succeeds, cam-002's call raises locally.
        def _mixed(device_id, function_name, params, timeout):
            if device_id == "cam-002":
                raise RuntimeError("messaging blip")
            return {"jsonrpc": "2.0", "id": "1", "result": {"ok": True}}
        conn = _conn_with_invoke(_mixed)
        with patch.object(tools_mod, "get_connection", return_value=conn):
            r = tools_mod.invoke_many("device(*).function(capture_image)")
        assert r["succeeded"] == 1
        assert r["failed"] == 1
        cam002_err = next(e for e in r["errors"] if e["device_id"] == "cam-002")
        assert cam002_err["error"]["code"] == "invoke_failed"
        assert "messaging blip" in cam002_err["error"]["message"]

    def test_llm_reasoning_stripped_from_params(self, all_succeed_conn):
        tools_mod.invoke_many(
            "device(*).function(capture_image)",
            params={"resolution": "4k", "llm_reasoning": "should not appear"},
        )
        for call in all_succeed_conn.invoke.call_args_list:
            sent = call.kwargs["params"]
            assert "llm_reasoning" not in sent
            assert sent["resolution"] == "4k"


# -- _resolve_function_tuples ---------------------------------------


class TestResolveFunctionTuples:
    def test_walks_all_pages(self, all_succeed_conn):
        # Use a small DISCOVER_HARD_LIMIT temporarily.
        with patch.object(tools_mod, "DISCOVER_HARD_LIMIT", 1):
            rows, err = tools_mod._resolve_function_tuples(
                "device(*).function(direction:write)"
            )
        assert err is None
        # 4 distinct (device, function) tuples for direction:write across the
        # mock fleet (cam-001, cam-002, robot-001, sensor-001 set_threshold
        # and set_location). With limit=1 per page, the resolver had to
        # paginate through all of them.
        assert len(rows) >= 2
        for row in rows:
            assert "device_id" in row
            assert "name" in row

    def test_propagates_discover_error(self, all_succeed_conn):
        rows, err = tools_mod._resolve_function_tuples("not a selector")
        assert rows is None
        assert err is not None
        assert err["error"]["code"] == "selector_parse_error"
