"""Unit tests for device_connect_container.sidecar_runtime module.

Tests cover:
- Capability loading from directory
- Function collection from @rpc methods
- JSON-RPC command handling (_describe, regular methods, errors)
- Event emission wiring
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from device_connect_container.sidecar_runtime import CapabilitySidecarRuntime


# -- Helpers --


SIDECAR_CAP_CODE = """\
from device_connect_edge.drivers.decorators import rpc, emit

class TestCap:
    def __init__(self, device=None):
        self.device = device

    @rpc()
    async def greet(self, name: str = "world") -> dict:
        \"\"\"Say hello.\"\"\"
        return {"message": f"Hello, {name}!"}

    @rpc()
    async def fail(self) -> dict:
        \"\"\"Always fails.\"\"\"
        raise ValueError("Intentional error")

    @emit()
    async def greeting_sent(self, target: str):
        \"\"\"Greeting was sent.\"\"\"
        pass
"""


def _write_sidecar_cap(tmp_path):
    """Write a capability directory for sidecar testing."""
    manifest = {
        "id": "test-cap",
        "class_name": "TestCap",
        "entry_point": "capability.py",
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "capability.py").write_text(SIDECAR_CAP_CODE)


# -- CapabilitySidecarRuntime loading --


class TestSidecarRuntimeLoading:
    def test_load_capability(self, tmp_path):
        _write_sidecar_cap(tmp_path)
        runtime = CapabilitySidecarRuntime(
            capability_dir=tmp_path,
            device_id="dev-1",
        )
        runtime._load_capability()

        assert runtime._capability_id == "test-cap"
        assert "greet" in runtime._functions
        assert "fail" in runtime._functions

    def test_load_missing_manifest_raises(self, tmp_path):
        runtime = CapabilitySidecarRuntime(
            capability_dir=tmp_path,
            device_id="dev-1",
        )
        with pytest.raises(FileNotFoundError, match="No manifest.json"):
            runtime._load_capability()

    def test_function_schemas_collected(self, tmp_path):
        _write_sidecar_cap(tmp_path)
        runtime = CapabilitySidecarRuntime(
            capability_dir=tmp_path,
            device_id="dev-1",
        )
        runtime._load_capability()

        assert "greet" in runtime._function_schemas
        schema = runtime._function_schemas["greet"]
        assert "description" in schema
        assert "parameters" in schema


# -- Command handling --


class TestSidecarRuntimeCommands:
    @pytest.fixture
    def runtime(self, tmp_path):
        _write_sidecar_cap(tmp_path)
        rt = CapabilitySidecarRuntime(
            capability_dir=tmp_path,
            device_id="dev-1",
            tenant="test",
        )
        rt._load_capability()
        rt._messaging = AsyncMock()
        return rt

    @pytest.mark.asyncio
    async def test_handle_describe(self, runtime):
        request = {
            "jsonrpc": "2.0",
            "id": "desc-1",
            "method": "_describe",
            "params": {},
        }
        await runtime._handle_command(
            json.dumps(request).encode(),
        )

        runtime._messaging.publish.assert_awaited_once()
        response = json.loads(runtime._messaging.publish.call_args[0][1])
        assert response["id"] == "desc-1"
        assert "test-cap" == response["result"]["capability_id"]
        assert "greet" in response["result"]["functions"]

    @pytest.mark.asyncio
    async def test_handle_rpc_success(self, runtime):
        request = {
            "jsonrpc": "2.0",
            "id": "rpc-1",
            "method": "greet",
            "params": {"name": "Alice"},
        }
        await runtime._handle_command(
            json.dumps(request).encode(),
        )

        response = json.loads(runtime._messaging.publish.call_args[0][1])
        assert response["result"] == {"message": "Hello, Alice!"}

    @pytest.mark.asyncio
    async def test_handle_rpc_error(self, runtime):
        request = {
            "jsonrpc": "2.0",
            "id": "rpc-2",
            "method": "fail",
            "params": {},
        }
        await runtime._handle_command(
            json.dumps(request).encode(),
        )

        response = json.loads(runtime._messaging.publish.call_args[0][1])
        assert "error" in response
        assert "Intentional error" in response["error"]["message"]

    @pytest.mark.asyncio
    async def test_handle_unknown_method(self, runtime):
        request = {
            "jsonrpc": "2.0",
            "id": "rpc-3",
            "method": "nonexistent",
            "params": {},
        }
        await runtime._handle_command(
            json.dumps(request).encode(),
        )

        response = json.loads(runtime._messaging.publish.call_args[0][1])
        assert response["error"]["code"] == -32601
        assert "Method not found" in response["error"]["message"]

    @pytest.mark.asyncio
    async def test_handle_invalid_json(self, runtime):
        await runtime._handle_command(
            b"not json",
        )
        # Should not crash; logs error but does not publish response
        runtime._messaging.publish.assert_not_awaited()


# -- Topic construction --


class TestSidecarRuntimeTopics:
    def test_cmd_subject(self, tmp_path):
        _write_sidecar_cap(tmp_path)
        rt = CapabilitySidecarRuntime(
            capability_dir=tmp_path,
            device_id="robot-001",
            tenant="lab",
        )
        rt._load_capability()
        assert rt.cmd_subject == "device-connect.lab.robot-001.cap.test-cap.cmd"

    def test_event_subject_prefix(self, tmp_path):
        _write_sidecar_cap(tmp_path)
        rt = CapabilitySidecarRuntime(
            capability_dir=tmp_path,
            device_id="d-1",
            tenant="default",
        )
        rt._load_capability()
        assert rt.event_subject_prefix == "device-connect.default.d-1.cap.test-cap.event"
