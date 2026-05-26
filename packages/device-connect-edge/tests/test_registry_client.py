# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for device_connect_edge.registry_client module.

Tests RegistryClient._request retry logic with mocked messaging.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from device_connect_edge.messaging.exceptions import RequestTimeoutError
from device_connect_edge.registry_client import RegistryClient


def _make_client(mock_messaging=None, **kwargs):
    """Create a RegistryClient with a mocked messaging client."""
    messaging = mock_messaging or AsyncMock()
    return RegistryClient(messaging, **kwargs), messaging


def _success_response(result=None):
    """Return bytes for a successful JSON-RPC response."""
    return json.dumps({
        "jsonrpc": "2.0",
        "id": "rpc-test",
        "result": result or {"ok": True},
    }).encode()


def _error_response(code=-32601, message="Method not found"):
    """Return bytes for a JSON-RPC error response."""
    return json.dumps({
        "jsonrpc": "2.0",
        "id": "rpc-test",
        "error": {"code": code, "message": message},
    }).encode()


class TestRequestRetries:
    @pytest.mark.asyncio
    @patch("device_connect_edge.registry_client.asyncio.sleep", new_callable=AsyncMock)
    async def test_request_retries_on_timeout(self, mock_sleep):
        """_request retries on RequestTimeoutError and returns result on success."""
        client, messaging = _make_client()
        messaging.request = AsyncMock(
            side_effect=[
                RequestTimeoutError("timeout 1"),
                RequestTimeoutError("timeout 2"),
                _success_response({"devices": []}),
            ]
        )

        result = await client._request(
            "device-connect.default.discovery",
            "discovery/listDevices",
            retries=3,
        )

        assert result == {"devices": []}
        assert messaging.request.call_count == 3

    @pytest.mark.asyncio
    async def test_request_no_retry_on_runtime_error(self):
        """_request raises RuntimeError immediately on JSON-RPC error (no retries)."""
        client, messaging = _make_client()
        messaging.request = AsyncMock(
            return_value=_error_response(-32601, "Method not found"),
        )

        with pytest.raises(RuntimeError, match="Method not found"):
            await client._request(
                "device-connect.default.discovery",
                "discovery/badMethod",
                retries=3,
            )

        # Should only be called once — no retries on server-side errors
        assert messaging.request.call_count == 1

    @pytest.mark.asyncio
    @patch("device_connect_edge.registry_client.asyncio.sleep", new_callable=AsyncMock)
    async def test_request_raises_after_all_retries_exhausted(self, mock_sleep):
        """_request raises RequestTimeoutError after exhausting all retries."""
        client, messaging = _make_client()
        messaging.request = AsyncMock(
            side_effect=RequestTimeoutError("always times out"),
        )

        with pytest.raises(RequestTimeoutError):
            await client._request(
                "device-connect.default.discovery",
                "discovery/listDevices",
                retries=3,
            )

        assert messaging.request.call_count == 3


class TestListDevicesPagination:
    """Verify list_devices transparently pages through the registry."""

    @staticmethod
    def _paged_responses(total: int, page_size: int):
        """Build the sequence of NATS reply bytes the server would emit."""
        devices = [{"device_id": f"dev-{i:04d}"} for i in range(total)]
        responses = []
        for start in range(0, total, page_size):
            page = devices[start:start + page_size]
            end = start + page_size
            next_offset = end if end < total else None
            responses.append(json.dumps({
                "jsonrpc": "2.0",
                "id": "rpc-test",
                "result": {
                    "devices": page,
                    "next_offset": next_offset,
                    "total_matched": total,
                },
            }).encode())
        if not responses:
            # Empty fleet: still need one round-trip
            responses.append(json.dumps({
                "jsonrpc": "2.0",
                "id": "rpc-test",
                "result": {"devices": [], "next_offset": None, "total_matched": 0},
            }).encode())
        return responses

    @pytest.mark.asyncio
    async def test_list_devices_pages_through_full_fleet(self):
        """1400 devices should arrive across multiple round-trips."""
        client, messaging = _make_client()
        messaging.request = AsyncMock(side_effect=self._paged_responses(1400, 100))

        devices = await client.list_devices()

        assert len(devices) == 1400
        assert [d["device_id"] for d in devices] == [
            f"dev-{i:04d}" for i in range(1400)
        ]
        # 1400 / 100 = 14 round-trips
        assert messaging.request.call_count == 14

    @pytest.mark.asyncio
    async def test_list_devices_passes_offset_and_limit_in_params(self):
        """Each request must carry the pagination params on the wire."""
        client, messaging = _make_client()
        messaging.request = AsyncMock(side_effect=self._paged_responses(250, 100))

        await client.list_devices()

        offsets = []
        limits = []
        for call_args in messaging.request.call_args_list:
            payload = json.loads(call_args.args[1])
            offsets.append(payload["params"]["offset"])
            limits.append(payload["params"]["limit"])

        assert offsets == [0, 100, 200]
        assert all(lim == 100 for lim in limits)

    @pytest.mark.asyncio
    async def test_list_devices_legacy_server_single_reply(self):
        """Server without pagination (no next_offset) terminates after 1 call."""
        client, messaging = _make_client()
        # Legacy reply shape: devices only, no pagination metadata.
        legacy = json.dumps({
            "jsonrpc": "2.0",
            "id": "rpc-test",
            "result": {"devices": [{"device_id": "a"}, {"device_id": "b"}]},
        }).encode()
        messaging.request = AsyncMock(return_value=legacy)

        devices = await client.list_devices()

        assert len(devices) == 2
        # next_offset absent => loop exits after one request
        assert messaging.request.call_count == 1

    @pytest.mark.asyncio
    async def test_list_devices_page_returns_metadata(self):
        """list_devices_page exposes next_offset and total_matched to caller."""
        client, messaging = _make_client()
        reply = json.dumps({
            "jsonrpc": "2.0",
            "id": "rpc-test",
            "result": {
                "devices": [{"device_id": "a"}, {"device_id": "b"}],
                "next_offset": 2,
                "total_matched": 10,
            },
        }).encode()
        messaging.request = AsyncMock(return_value=reply)

        page, next_offset, total = await client.list_devices_page(
            offset=0, limit=2,
        )

        assert len(page) == 2
        assert next_offset == 2
        assert total == 10

    @pytest.mark.asyncio
    async def test_list_devices_forwards_filters(self):
        """device_type / location filters must accompany pagination params."""
        client, messaging = _make_client()
        messaging.request = AsyncMock(side_effect=self._paged_responses(0, 100))

        await client.list_devices(device_type="camera", location="lab-A")

        payload = json.loads(messaging.request.call_args.args[1])
        assert payload["params"]["device_type"] == "camera"
        assert payload["params"]["location"] == "lab-A"
        assert payload["params"]["offset"] == 0
        assert payload["params"]["limit"] == 100

    @pytest.mark.asyncio
    async def test_list_devices_handles_empty_page_with_next_offset(self):
        """ACL filtering can yield an empty page mid-walk with next_offset
        still pointing forward; the loop must advance, not stall."""
        client, messaging = _make_client()
        responses = [
            # Page 0: ACL filtered everything out, but more pages follow.
            json.dumps({
                "jsonrpc": "2.0",
                "id": "rpc-test",
                "result": {"devices": [], "next_offset": 100, "total_matched": 200},
            }).encode(),
            # Page 1: some visible devices, final page.
            json.dumps({
                "jsonrpc": "2.0",
                "id": "rpc-test",
                "result": {
                    "devices": [{"device_id": "visible-1"}],
                    "next_offset": None,
                    "total_matched": 200,
                },
            }).encode(),
        ]
        messaging.request = AsyncMock(side_effect=responses)

        devices = await client.list_devices()

        assert [d["device_id"] for d in devices] == ["visible-1"]
        assert messaging.request.call_count == 2
        # Second request must use next_offset from the first reply.
        second_payload = json.loads(messaging.request.call_args_list[1].args[1])
        assert second_payload["params"]["offset"] == 100

    @pytest.mark.asyncio
    async def test_list_devices_breaks_on_non_advancing_next_offset(self, caplog):
        """A buggy server returning next_offset <= current offset must not
        spin the client forever — the page loop bails with a warning."""
        client, messaging = _make_client()
        # Server bug: keeps returning the same offset.
        repeating = json.dumps({
            "jsonrpc": "2.0",
            "id": "rpc-test",
            "result": {
                "devices": [{"device_id": "a"}],
                "next_offset": 0,
                "total_matched": 100,
            },
        }).encode()
        messaging.request = AsyncMock(return_value=repeating)

        with caplog.at_level("WARNING"):
            devices = await client.list_devices()

        assert len(devices) == 1
        assert messaging.request.call_count == 1
        assert any(
            "non-advancing next_offset" in rec.message for rec in caplog.records
        )
