# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""State discovery uses stored records and paginates only the matches."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from device_connect_server.registry.service import registry
from device_connect_server.registry.service.main import _make_list_handler
from device_connect_server.security.acl import ACLManager, DeviceACL


def stage(index, **status):
    return {
        "device_id": f"stage-{index:04d}",
        "identity": {"device_type": "ophyd_async:SimStage"},
        "capabilities": {"labels": {"model_id": "ophyd_async:SimStage"}, "functions": []},
        "status": {"location": "lab-A", "x_readback": float(index), "x_setpoint": 2.0, **status},
    }


@pytest.fixture
def fleet(monkeypatch):
    records = [stage(i) for i in range(6)]
    monkeypatch.setattr(registry._REGISTRY, "_decoded_fleet", lambda tenant: records)
    return records


def test_where_filters_before_pagination(fleet):
    page, cursor, total = registry.list_devices_page(
        "default", where="status.x_readback >= 2.0", offset=1, limit=2,
    )
    assert [d["device_id"] for d in page] == ["stage-0003", "stage-0004"]
    assert (cursor, total) == (3, 4)
    page, cursor, total = registry.list_devices_page(
        "default", where="status.x_readback >= 2.0", offset=3, limit=2,
    )
    assert [d["device_id"] for d in page] == ["stage-0005"]
    assert (cursor, total) == (None, 4)


def test_where_binds_identity_labels_and_status_without_mutating_records(fleet):
    original = json.dumps(fleet)
    page = registry.list_devices(
        "default", device_type="SimStage", location="lab",
        where=("identity.device_id == 'stage-0002' && "
               "identity.device_type == 'ophyd_async:SimStage' && "
               "labels.model_id == 'ophyd_async:SimStage' && "
               "labels.location == 'lab-A' && labels.type == identity.device_type && "
               "status.x_setpoint == 2.0"),
    )
    assert [d["device_id"] for d in page] == ["stage-0002"]
    assert json.dumps(fleet) == original


def test_where_missing_or_incompatible_status_does_not_match(fleet):
    fleet[0]["status"] = {}
    fleet[1]["status"]["x_readback"] = "unknown"
    page = registry.list_devices("default", where="status.x_readback < 3.0")
    assert [d["device_id"] for d in page] == ["stage-0002"]


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["status.x_readback > > 1", "", " ", 42, False, {}])
async def test_malformed_where_returns_invalid_params_even_for_empty_fleet(monkeypatch, where):
    monkeypatch.setattr(registry._REGISTRY, "_decoded_fleet", lambda tenant: [])
    messaging = AsyncMock()
    request = {"id": "bad-where", "method": "discovery/listDevices", "params": {"where": where, "limit": 10}}
    await _make_list_handler("default", messaging)(json.dumps(request).encode(), "reply")
    response = json.loads(messaging.publish.call_args.args[1])
    assert response["id"] == "bad-where"
    assert response["error"]["code"] == -32602
    assert "where" in response["error"]["message"]


@pytest.mark.asyncio
async def test_five_thousand_functionless_devices_return_one_small_reply(monkeypatch):
    records = [stage(i, x_setpoint=2.0 if i == 51 else 0.0) for i in range(5000)]
    monkeypatch.setattr(registry._REGISTRY, "_decoded_fleet", lambda tenant: records)
    messaging = AsyncMock()
    request = {
        "id": "state-query", "method": "discovery/listDevices",
        "params": {"where": "status.x_readback < 51.99 && status.x_setpoint == 2.0", "limit": 200},
    }
    with patch.object(registry, "compile_where", wraps=registry.compile_where) as compile_predicate:
        await _make_list_handler("default", messaging)(json.dumps(request).encode(), "reply")
    compile_predicate.assert_called_once()
    response_bytes = messaging.publish.call_args.args[1]
    result = json.loads(response_bytes)["result"]
    assert result["total_matched"] == 1
    assert result["next_offset"] is None
    assert result["where_applied"] is True
    assert [d["device_id"] for d in result["devices"]] == ["stage-0051"]
    assert len(response_bytes) < 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("where,offset,expected_ids,cursor,total", [
    ("status.x_readback >= 0.0", 0, ["stage-0001", "stage-0003"], 2, 3),
    ("status.x_readback >= 0.0", 2, ["stage-0005"], None, 3),
    ("identity.device_id == 'stage-0000' && status.x_setpoint == 2.0", 0, [], None, 0),
])
async def test_where_counts_and_pages_only_acl_visible_matches(fleet, where, offset, expected_ids, cursor, total):
    acl = ACLManager()
    for index in (0, 2, 4):
        acl.set_acl(DeviceACL(device_id=f"stage-{index:04d}", tenant="default", hidden_from=["observer"]))
    messaging = AsyncMock()
    request = {"id": "private-state", "method": "discovery/listDevices",
               "params": {"where": where, "offset": offset, "limit": 2, "requester_id": "observer"}}
    await _make_list_handler("default", messaging, acl)(json.dumps(request).encode(), "reply")
    result = json.loads(messaging.publish.call_args.args[1])["result"]
    assert [d["device_id"] for d in result["devices"]] == expected_ids
    assert (result["next_offset"], result["total_matched"]) == (cursor, total)
