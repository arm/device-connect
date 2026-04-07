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

