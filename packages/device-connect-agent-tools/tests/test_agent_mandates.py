# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Agent-tool tests for carrying Device Mandates in _dc_meta."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from device_connect_agent_tools import tools as tools_mod


SAMPLE_DEVICES = [
    {
        "device_id": "lock-001",
        "device_type": "lock",
        "status": {"state": "online"},
        "identity": {"device_type": "lock"},
        "labels": {"category": "lock"},
        "functions": [
            {
                "name": "unlock",
                "parameters": {},
                "labels": {"direction": "write", "safety": "critical"},
                "mandate": {"required": True, "scope": "actuation"},
            },
        ],
        "events": [],
    },
    {
        "device_id": "lock-002",
        "device_type": "lock",
        "status": {"state": "online"},
        "identity": {"device_type": "lock"},
        "labels": {"category": "lock"},
        "functions": [
            {
                "name": "unlock",
                "parameters": {},
                "labels": {"direction": "write", "safety": "critical"},
                "mandate": {"required": True, "scope": "actuation"},
            },
        ],
        "events": [],
    },
]


MANDATE = {"format": "device-connect-hmac-v0", "closed": {"id": "closed-1"}}


def _conn():
    conn = MagicMock()
    conn.list_devices.return_value = SAMPLE_DEVICES
    conn.invoke.return_value = {"jsonrpc": "2.0", "id": "1", "result": {"ok": True}}
    conn._published = []
    conn.publish_broadcast.side_effect = lambda env: conn._published.append(env)
    return conn


def test_invoke_attaches_mandate_under_dc_meta():
    conn = _conn()
    with patch.object(tools_mod, "get_connection", return_value=conn):
        result = tools_mod.invoke(
            "device(lock-001).function(unlock)",
            params={"duration_s": 30},
            mandate=MANDATE,
        )

    assert result["success"] is True
    sent = conn.invoke.call_args.kwargs["params"]
    assert sent["duration_s"] == 30
    assert sent["_dc_meta"]["mandate"] == MANDATE


def test_invoke_many_attaches_mandate_to_each_call():
    conn = _conn()
    with patch.object(tools_mod, "get_connection", return_value=conn):
        tools_mod.invoke_many(
            "device(category:lock).function(unlock)",
            params={"duration_s": 30},
            mandate=MANDATE,
        )

    assert conn.invoke.call_count == 2
    for call in conn.invoke.call_args_list:
        assert call.kwargs["params"]["_dc_meta"]["mandate"] == MANDATE


def test_broadcast_attaches_mandate_under_params_dc_meta():
    conn = _conn()
    with patch.object(tools_mod, "get_connection", return_value=conn):
        result = tools_mod.broadcast(
            "device(category:lock).function(unlock)",
            params={"duration_s": 30},
            mandate=MANDATE,
        )

    assert result["candidates"] == 2
    env = conn._published[0]
    assert env["params"]["duration_s"] == 30
    assert env["params"]["_dc_meta"]["mandate"] == MANDATE


def test_legacy_invoke_device_attaches_mandate_under_dc_meta():
    conn = _conn()
    with patch.object(tools_mod, "get_connection", return_value=conn):
        tools_mod.invoke_device(
            "lock-001",
            "unlock",
            params={"duration_s": 30},
            mandate=MANDATE,
        )

    sent = conn.invoke.call_args.kwargs["params"]
    assert sent["_dc_meta"]["mandate"] == MANDATE
