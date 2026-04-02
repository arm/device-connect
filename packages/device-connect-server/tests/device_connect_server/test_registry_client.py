"""Unit tests for device_connect_server.registry.client.RegistryClient.

Tests use a mocked MessagingClient — no real NATS required.
"""

import json
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from device_connect_server.registry.client import RegistryClient


# ── Helpers ───────────────────────────────────────────────────────

def _mock_messaging(response_payload: dict) -> MagicMock:
    """Create a mock MessagingClient that returns the given payload."""
    mc = MagicMock()
    type(mc).is_connected = PropertyMock(return_value=True)
    mc.request = AsyncMock(return_value=json.dumps(response_payload).encode())
    mc.close = AsyncMock()
    mc.connect = AsyncMock()
    return mc


SAMPLE_DEVICES = [
    {
        "device_id": "camera-001",
        "device_type": "camera",
        "location": "lab-A",
        "base": {
            "functions": [{"name": "capture_image", "description": "Capture image"}],
            "events": [{"name": "state_change_detected"}],
        },
    },
    {
        "device_id": "robot-001",
        "device_type": "robot",
        "location": "lab-B",
        "base": {
            "functions": [{"name": "dispatch_robot"}],
            "events": [{"name": "cleaning_finished"}],
        },
    },
]


# ── Connection lifecycle ──────────────────────────────────────────


class TestRegistryClientConnection:
    @pytest.mark.asyncio
    async def test_connect_when_already_connected(self):
        mc = _mock_messaging({"result": {}})
        client = RegistryClient(mc)
        await client.connect()
        mc.connect.assert_not_called()  # Already connected

    @pytest.mark.asyncio
    async def test_connect_when_not_connected(self):
        mc = _mock_messaging({"result": {}})
        type(mc).is_connected = PropertyMock(return_value=False)
        client = RegistryClient(mc)
        await client.connect()
        mc.connect.assert_called_once()

    @pytest.mark.asyncio
    async def test_close(self):
        mc = _mock_messaging({"result": {}})
        client = RegistryClient(mc)
        await client.connect()
        await client.close()
        mc.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_context_manager(self):
        mc = _mock_messaging({"result": {}})
        async with RegistryClient(mc) as _client:
            pass
        mc.close.assert_called_once()


# ── list_devices ──────────────────────────────────────────────────


class TestListDevices:
    @pytest.mark.asyncio
    async def test_list_all(self):
        mc = _mock_messaging({"result": {"devices": SAMPLE_DEVICES}})
        client = RegistryClient(mc, tenant="test")
        await client.connect()

        devices = await client.list_devices()

        assert len(devices) == 2
        assert devices[0]["device_id"] == "camera-001"
        assert devices[1]["device_id"] == "robot-001"

    @pytest.mark.asyncio
    async def test_list_by_type(self):
        mc = _mock_messaging({"result": {"devices": SAMPLE_DEVICES}})
        client = RegistryClient(mc, tenant="test")
        await client.connect()

        await client.list_devices(device_type="camera")

        # Verify the params sent in the request
        call_args = mc.request.call_args
        payload = json.loads(call_args[0][1])
        assert payload["params"]["device_type"] == "camera"

    @pytest.mark.asyncio
    async def test_list_uses_correct_subject(self):
        mc = _mock_messaging({"result": {"devices": []}})
        client = RegistryClient(mc, tenant="my-zone")
        await client.connect()

        await client.list_devices()

        call_args = mc.request.call_args
        assert call_args[0][0] == "device-connect.my-zone.discovery"

    @pytest.mark.asyncio
    async def test_list_empty(self):
        mc = _mock_messaging({"result": {"devices": []}})
        client = RegistryClient(mc)
        await client.connect()

        devices = await client.list_devices()
        assert devices == []


# ── get_device ────────────────────────────────────────────────────


class TestGetDevice:
    @pytest.mark.asyncio
    async def test_get_existing(self):
        mc = _mock_messaging({"result": {"device": SAMPLE_DEVICES[1]}})
        client = RegistryClient(mc)
        await client.connect()

        device = await client.get_device("robot-001")

        assert device is not None
        assert device["device_id"] == "robot-001"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self):
        mc = _mock_messaging({"error": {"code": -32602, "message": "not found"}})
        client = RegistryClient(mc)
        await client.connect()

        device = await client.get_device("nonexistent")
        assert device is None


# ── get_device_functions / get_device_events ──────────────────────


class TestGetDeviceFunctionsEvents:
    @pytest.mark.asyncio
    async def test_get_functions(self):
        mc = _mock_messaging({"result": {"device": SAMPLE_DEVICES[0]}})
        client = RegistryClient(mc)
        await client.connect()

        functions = await client.get_device_functions("camera-001")

        assert len(functions) == 1
        assert functions[0]["name"] == "capture_image"

    @pytest.mark.asyncio
    async def test_get_events(self):
        mc = _mock_messaging({"result": {"device": SAMPLE_DEVICES[0]}})
        client = RegistryClient(mc)
        await client.connect()

        events = await client.get_device_events("camera-001")

        assert len(events) == 1
        assert events[0]["name"] == "state_change_detected"

    @pytest.mark.asyncio
    async def test_get_functions_nonexistent_device(self):
        mc = _mock_messaging({"error": {"code": -32602, "message": "not found"}})
        client = RegistryClient(mc)
        await client.connect()

        functions = await client.get_device_functions("nonexistent")
        assert functions == []

    @pytest.mark.asyncio
    async def test_get_events_nonexistent_device(self):
        mc = _mock_messaging({"error": {"code": -32602, "message": "not found"}})
        client = RegistryClient(mc)
        await client.connect()

        events = await client.get_device_events("nonexistent")
        assert events == []


# ── Error handling ────────────────────────────────────────────────


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_rpc_error(self):
        mc = _mock_messaging({
            "error": {"code": -32601, "message": "Method not found"},
        })
        client = RegistryClient(mc)
        await client.connect()

        with pytest.raises(RuntimeError, match="Method not found"):
            await client.list_devices()

    @pytest.mark.asyncio
    async def test_request_sends_jsonrpc_format(self):
        mc = _mock_messaging({"result": {"devices": []}})
        client = RegistryClient(mc)
        await client.connect()

        await client.list_devices()

        call_args = mc.request.call_args
        payload = json.loads(call_args[0][1])
        assert payload["jsonrpc"] == "2.0"
        assert "id" in payload
        assert payload["method"] == "discovery/listDevices"
