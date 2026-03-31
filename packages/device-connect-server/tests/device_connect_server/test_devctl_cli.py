"""Tests for device_connect_server.devctl.cli module."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from device_connect_server.devctl.cli import (
    print_compact_devices,
    list_devices,
    _create_messaging_client,
)


# ── Helper tests ──────────────────────────────────────────────────


class TestPrintCompactDevices:
    def test_print_functions_and_events(self, capsys):
        devices = [
            {
                "device_id": "cam-001",
                "capabilities": {
                    "functions": [{"name": "capture", "description": "Capture image"}],
                    "events": [{"name": "motion", "description": "Motion detected"}],
                },
            },
        ]
        print_compact_devices(devices)
        out = capsys.readouterr().out
        assert "cam-001" in out
        assert "function capture" in out
        assert "event motion" in out

    def test_empty_list(self, capsys):
        print_compact_devices([])
        out = capsys.readouterr().out
        assert out == ""

    def test_missing_capabilities(self, capsys):
        print_compact_devices([{"device_id": "stub"}])
        out = capsys.readouterr().out
        assert "stub" in out


# ── _create_messaging_client ──────────────────────────────────────


class TestCreateMessagingClient:
    @patch("device_connect_server.devctl.cli.create_client")
    def test_returns_client_and_config(self, mock_create):
        mock_create.return_value = MagicMock()
        messaging, config = _create_messaging_client()
        assert messaging is not None
        assert config is not None
        mock_create.assert_called_once()


# ── list_devices ──────────────────────────────────────────────────


class TestListDevices:
    @pytest.mark.asyncio
    @patch("device_connect_server.devctl.cli.create_client")
    async def test_list_devices_returns_list(self, mock_create):
        mock_messaging = AsyncMock()
        mock_create.return_value = mock_messaging

        sample_devices = [
            {"device_id": "cam-001", "capabilities": {}},
            {"device_id": "robot-001", "capabilities": {}},
        ]
        mock_messaging.request.return_value = json.dumps({
            "result": {"devices": sample_devices}
        }).encode()

        devices = await list_devices(messaging_url="nats://localhost:4222")
        assert len(devices) == 2
        assert devices[0]["device_id"] == "cam-001"
        mock_messaging.close.assert_called_once()

    @pytest.mark.asyncio
    @patch("device_connect_server.devctl.cli.create_client")
    async def test_list_devices_compact(self, mock_create, capsys):
        mock_messaging = AsyncMock()
        mock_create.return_value = mock_messaging

        mock_messaging.request.return_value = json.dumps({
            "result": {"devices": [
                {"device_id": "cam-001", "capabilities": {
                    "functions": [{"name": "snap", "description": ""}],
                    "events": [],
                }},
            ]}
        }).encode()

        await list_devices(messaging_url="nats://localhost:4222", compact=True)
        out = capsys.readouterr().out
        assert "cam-001" in out
        assert "function snap" in out

    @pytest.mark.asyncio
    @patch("device_connect_server.devctl.cli.create_client")
    async def test_list_devices_error_propagates(self, mock_create):
        mock_messaging = AsyncMock()
        mock_create.return_value = mock_messaging
        mock_messaging.request.side_effect = TimeoutError("no response")

        with pytest.raises(TimeoutError):
            await list_devices(messaging_url="nats://localhost:4222")
        mock_messaging.close.assert_called_once()
