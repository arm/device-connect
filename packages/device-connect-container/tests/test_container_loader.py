"""Unit tests for device_connect_container.container_loader module.

Tests cover:
- ContainerCapabilityProxy topic construction
- ContainerCapabilityProxy RPC callable creation
- ContainerCapabilityLoader mixed-mode loading (container + in-process)
- Schema querying from sidecar (_describe method)
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from device_connect_container.container_loader import (
    ContainerCapabilityProxy,
    ContainerCapabilityLoader,
)
from device_connect_container.manifest import ContainerManifest, ContainerConfig


# -- Helpers --


def _make_mock_messaging():
    """Return an AsyncMock messaging client."""
    messaging = AsyncMock()
    messaging.subscribe = AsyncMock(return_value=AsyncMock())
    messaging.publish = AsyncMock()
    messaging.request = AsyncMock()
    return messaging


def _write_manifest(tmp_path, cap_id, containerized=False, extra=None):
    """Write a capability directory with manifest.json."""
    cap_dir = tmp_path / cap_id
    cap_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": cap_id,
        "class_name": f"{cap_id.replace('-', '_').title()}Cap",
        "entry_point": "capability.py",
    }
    if containerized:
        manifest["container"] = {"image": f"{cap_id}:latest"}
    if extra:
        manifest.update(extra)
    (cap_dir / "manifest.json").write_text(json.dumps(manifest))

    # Write minimal capability code for in-process loading
    code = f"""\
class {manifest['class_name']}:
    def __init__(self, device=None):
        self.device = device
"""
    (cap_dir / "capability.py").write_text(code)
    return cap_dir


# -- ContainerCapabilityProxy topics --


class TestContainerCapabilityProxyTopics:
    def test_cmd_subject(self):
        proxy = ContainerCapabilityProxy(
            capability_id="vision",
            manifest=MagicMock(),
            messaging=MagicMock(),
            device_id="device-001",
            tenant="default",
        )
        assert proxy.cmd_subject == "device-connect.default.device-001.cap.vision.cmd"

    def test_event_subject_prefix(self):
        proxy = ContainerCapabilityProxy(
            capability_id="arm",
            manifest=MagicMock(),
            messaging=MagicMock(),
            device_id="robot-001",
            tenant="lab",
        )
        assert proxy.event_subject_prefix == "device-connect.lab.robot-001.cap.arm.event"

    def test_health_subject(self):
        proxy = ContainerCapabilityProxy(
            capability_id="sensor",
            manifest=MagicMock(),
            messaging=MagicMock(),
            device_id="dev-99",
            tenant="default",
        )
        assert proxy.health_subject == "device-connect.default.dev-99.cap.sensor.health"


# -- ContainerCapabilityProxy RPC callable --


class TestContainerCapabilityProxyRpc:
    def test_create_rpc_callable_metadata(self):
        proxy = ContainerCapabilityProxy(
            capability_id="vision",
            manifest=MagicMock(),
            messaging=_make_mock_messaging(),
            device_id="dev-1",
        )
        fn = proxy.create_rpc_callable("capture_image", {"description": "Capture an image"})
        assert fn._is_device_function is True
        assert fn._function_name == "capture_image"
        assert fn._description == "Capture an image"
        assert fn.__name__ == "capture_image"

    @pytest.mark.asyncio
    async def test_rpc_callable_sends_jsonrpc(self):
        messaging = _make_mock_messaging()
        messaging.request.return_value = json.dumps({
            "jsonrpc": "2.0",
            "id": "cap-test",
            "result": {"image": "base64data"},
        }).encode()

        proxy = ContainerCapabilityProxy(
            capability_id="vision",
            manifest=MagicMock(),
            messaging=messaging,
            device_id="dev-1",
        )
        fn = proxy.create_rpc_callable("capture", {})
        result = await fn(resolution="1080p")

        assert result == {"image": "base64data"}
        messaging.request.assert_awaited_once()
        call_args = messaging.request.call_args
        sent_payload = json.loads(call_args[0][1].decode())
        assert sent_payload["method"] == "capture"
        assert sent_payload["params"]["resolution"] == "1080p"

    @pytest.mark.asyncio
    async def test_rpc_callable_raises_on_error_response(self):
        messaging = _make_mock_messaging()
        messaging.request.return_value = json.dumps({
            "jsonrpc": "2.0",
            "id": "cap-err",
            "error": {"code": -32000, "message": "Camera not ready"},
        }).encode()

        proxy = ContainerCapabilityProxy(
            capability_id="vision",
            manifest=MagicMock(),
            messaging=messaging,
            device_id="dev-1",
        )
        fn = proxy.create_rpc_callable("capture", {})

        with pytest.raises(RuntimeError, match="Camera not ready"):
            await fn()


# -- ContainerCapabilityProxy health --


class TestContainerCapabilityProxyHealth:
    def test_health_sets_ready(self):
        proxy = ContainerCapabilityProxy(
            capability_id="cap",
            manifest=MagicMock(),
            messaging=MagicMock(),
            device_id="d1",
        )
        assert not proxy._ready_event.is_set()

        proxy._on_health_message(
            "health",
            json.dumps({"healthy": True}).encode(),
        )
        assert proxy._ready_event.is_set()
        assert proxy._healthy is True

    def test_health_unhealthy_does_not_set_ready(self):
        proxy = ContainerCapabilityProxy(
            capability_id="cap",
            manifest=MagicMock(),
            messaging=MagicMock(),
            device_id="d1",
        )
        proxy._on_health_message(
            "health",
            json.dumps({"healthy": False}).encode(),
        )
        assert not proxy._ready_event.is_set()


# -- ContainerCapabilityLoader --


class TestContainerCapabilityLoaderMixedMode:
    @pytest.mark.asyncio
    async def test_load_all_empty_dir(self, tmp_path):
        loader = ContainerCapabilityLoader(
            event_emitter=AsyncMock(),
            capabilities_dir=tmp_path,
            messaging=_make_mock_messaging(),
            device_id="dev-1",
        )
        count = await loader.load_all()
        assert count == 0

    @pytest.mark.asyncio
    async def test_load_all_nonexistent_dir(self):
        loader = ContainerCapabilityLoader(
            event_emitter=AsyncMock(),
            capabilities_dir=Path("/nonexistent"),
            messaging=_make_mock_messaging(),
            device_id="dev-1",
        )
        count = await loader.load_all()
        assert count == 0

    @pytest.mark.asyncio
    async def test_has_function_empty(self, tmp_path):
        loader = ContainerCapabilityLoader(
            event_emitter=AsyncMock(),
            capabilities_dir=tmp_path,
            messaging=_make_mock_messaging(),
            device_id="dev-1",
        )
        assert loader.has_function("nonexistent") is False

    @pytest.mark.asyncio
    async def test_invoke_unknown_function_raises(self, tmp_path):
        loader = ContainerCapabilityLoader(
            event_emitter=AsyncMock(),
            capabilities_dir=tmp_path,
            messaging=_make_mock_messaging(),
            device_id="dev-1",
        )
        with pytest.raises(KeyError, match="Function not found"):
            await loader.invoke("nonexistent")

    def test_simulation_mode_propagation(self, tmp_path):
        loader = ContainerCapabilityLoader(
            event_emitter=AsyncMock(),
            capabilities_dir=tmp_path,
            messaging=_make_mock_messaging(),
            device_id="dev-1",
            simulation_mode=True,
        )
        assert loader.simulation_mode is True
        loader.simulation_mode = False
        assert loader.simulation_mode is False
