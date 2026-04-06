"""Unit tests for device_connect_edge.drivers.capability_loader module.

Tests cover:
- Loading capabilities from a Python file via manifest
- Extracting @rpc and @emit methods from loaded capabilities
- Error handling for missing manifests, missing class_name, bad files
- CapabilityLoader invoke, has_function, get_functions
- Unloading capabilities
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from device_connect_edge.drivers.capability_loader import (
    CapabilityLoader,
)


# -- Helpers --


def _write_capability(tmp_path, cap_id, class_name, code, manifest_extra=None):
    """Write a capability directory with manifest.json and capability.py."""
    cap_dir = tmp_path / cap_id
    cap_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "id": cap_id,
        "entry_point": "capability.py",
        "class_name": class_name,
    }
    if manifest_extra:
        manifest.update(manifest_extra)

    (cap_dir / "manifest.json").write_text(json.dumps(manifest))
    (cap_dir / "capability.py").write_text(code)
    return cap_dir


SIMPLE_CAPABILITY_CODE = """\
from device_connect_edge.drivers.decorators import rpc, emit

class SimpleCapability:
    def __init__(self, device=None):
        self.device = device

    @rpc()
    async def ping(self) -> dict:
        \"\"\"Ping the capability.\"\"\"
        return {"pong": True}

    @rpc(name="customName")
    async def custom(self, value: str = "default") -> dict:
        \"\"\"A function with custom name.\"\"\"
        return {"value": value}
"""

EMIT_CAPABILITY_CODE = """\
from device_connect_edge.drivers.decorators import rpc, emit

class EmitCapability:
    def __init__(self, device=None):
        self.device = device

    @rpc()
    async def do_work(self) -> dict:
        \"\"\"Do some work.\"\"\"
        return {"done": True}

    @emit()
    async def work_complete(self, result: str):
        \"\"\"Work completed event.\"\"\"
        pass
"""

BAD_CAPABILITY_CODE = """\
raise ImportError("This module cannot be loaded")
"""


@pytest.fixture
def event_emitter():
    """Provide an async mock event emitter."""
    return AsyncMock()


@pytest.fixture
def loader(tmp_path, event_emitter):
    """Provide a CapabilityLoader pointed at tmp_path."""
    return CapabilityLoader(
        event_emitter=event_emitter,
        capabilities_dir=tmp_path,
        tenant="test-tenant",
    )


# -- CapabilityLoader basic loading --


class TestCapabilityLoaderLoadAll:
    @pytest.mark.asyncio
    async def test_load_all_empty_dir(self, loader, tmp_path):
        count = await loader.load_all()
        assert count == 0

    @pytest.mark.asyncio
    async def test_load_all_nonexistent_dir(self, event_emitter):
        loader = CapabilityLoader(
            event_emitter=event_emitter,
            capabilities_dir=Path("/nonexistent/path"),
        )
        count = await loader.load_all()
        assert count == 0

    @pytest.mark.asyncio
    async def test_load_single_capability(self, loader, tmp_path):
        _write_capability(tmp_path, "simple-cap", "SimpleCapability", SIMPLE_CAPABILITY_CODE)
        count = await loader.load_all()
        assert count == 1
        assert "simple-cap" in loader.get_capabilities()

    @pytest.mark.asyncio
    async def test_load_multiple_capabilities(self, loader, tmp_path):
        _write_capability(tmp_path, "cap-a", "SimpleCapability", SIMPLE_CAPABILITY_CODE)
        _write_capability(tmp_path, "cap-b", "EmitCapability", EMIT_CAPABILITY_CODE)
        count = await loader.load_all()
        assert count == 2


# -- Extracting @rpc methods --


class TestRpcExtraction:
    @pytest.mark.asyncio
    async def test_rpc_methods_registered(self, loader, tmp_path):
        _write_capability(tmp_path, "simple-cap", "SimpleCapability", SIMPLE_CAPABILITY_CODE)
        await loader.load_all()

        funcs = loader.get_functions()
        # Should register both with and without prefix
        assert "ping" in funcs
        assert "simple-cap.ping" in funcs
        assert "customName" in funcs
        assert "simple-cap.customName" in funcs

    @pytest.mark.asyncio
    async def test_has_function(self, loader, tmp_path):
        _write_capability(tmp_path, "simple-cap", "SimpleCapability", SIMPLE_CAPABILITY_CODE)
        await loader.load_all()

        assert loader.has_function("ping") is True
        assert loader.has_function("simple-cap.ping") is True
        assert loader.has_function("nonexistent") is False

    @pytest.mark.asyncio
    async def test_invoke_rpc(self, loader, tmp_path):
        _write_capability(tmp_path, "simple-cap", "SimpleCapability", SIMPLE_CAPABILITY_CODE)
        await loader.load_all()

        result = await loader.invoke("ping")
        assert result == {"pong": True}

    @pytest.mark.asyncio
    async def test_invoke_rpc_with_params(self, loader, tmp_path):
        _write_capability(tmp_path, "simple-cap", "SimpleCapability", SIMPLE_CAPABILITY_CODE)
        await loader.load_all()

        result = await loader.invoke("customName", value="hello")
        assert result == {"value": "hello"}

    @pytest.mark.asyncio
    async def test_invoke_unknown_function_raises(self, loader, tmp_path):
        _write_capability(tmp_path, "simple-cap", "SimpleCapability", SIMPLE_CAPABILITY_CODE)
        await loader.load_all()

        with pytest.raises(KeyError, match="nonexistent"):
            await loader.invoke("nonexistent")

    @pytest.mark.asyncio
    async def test_loaded_capability_functions_list(self, loader, tmp_path):
        _write_capability(tmp_path, "simple-cap", "SimpleCapability", SIMPLE_CAPABILITY_CODE)
        await loader.load_all()

        caps = loader.get_capabilities()
        loaded = caps["simple-cap"]
        assert "ping" in loaded.functions
        assert "customName" in loaded.functions

    @pytest.mark.asyncio
    async def test_function_schemas_populated(self, loader, tmp_path):
        _write_capability(tmp_path, "simple-cap", "SimpleCapability", SIMPLE_CAPABILITY_CODE)
        await loader.load_all()

        caps = loader.get_capabilities()
        loaded = caps["simple-cap"]
        assert "ping" in loaded.function_schemas
        assert "customName" in loaded.function_schemas
        schema = loaded.function_schemas["customName"]
        assert "parameters" in schema
        assert "description" in schema


# -- Extracting @emit methods --


class TestEmitExtraction:
    @pytest.mark.asyncio
    async def test_emit_methods_wired(self, loader, tmp_path):
        _write_capability(tmp_path, "emit-cap", "EmitCapability", EMIT_CAPABILITY_CODE)
        await loader.load_all()

        caps = loader.get_capabilities()
        loaded = caps["emit-cap"]
        instance = loaded.instance
        # The loader should have injected _dispatch_internal_event and _emit_event_internal
        assert hasattr(instance, "_dispatch_internal_event")
        assert hasattr(instance, "_emit_event_internal")

    @pytest.mark.asyncio
    async def test_rpc_alongside_emit(self, loader, tmp_path):
        _write_capability(tmp_path, "emit-cap", "EmitCapability", EMIT_CAPABILITY_CODE)
        await loader.load_all()

        # The @rpc method should be registered as a function
        assert loader.has_function("do_work")
        result = await loader.invoke("do_work")
        assert result == {"done": True}


# -- Error handling --


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_missing_manifest(self, loader, tmp_path):
        cap_dir = tmp_path / "bad-cap"
        cap_dir.mkdir()
        (cap_dir / "capability.py").write_text("class Foo: pass")
        # No manifest.json
        count = await loader.load_all()
        assert count == 0

    @pytest.mark.asyncio
    async def test_missing_class_name_in_manifest(self, loader, tmp_path):
        cap_dir = tmp_path / "no-class"
        cap_dir.mkdir()
        (cap_dir / "manifest.json").write_text(json.dumps({
            "id": "no-class",
            "entry_point": "capability.py",
            # no class_name
        }))
        (cap_dir / "capability.py").write_text("class Foo: pass")
        count = await loader.load_all()
        assert count == 0

    @pytest.mark.asyncio
    async def test_missing_entry_point_file(self, loader, tmp_path):
        cap_dir = tmp_path / "no-file"
        cap_dir.mkdir()
        (cap_dir / "manifest.json").write_text(json.dumps({
            "id": "no-file",
            "entry_point": "nonexistent.py",
            "class_name": "Foo",
        }))
        count = await loader.load_all()
        assert count == 0

    @pytest.mark.asyncio
    async def test_bad_capability_code(self, loader, tmp_path):
        _write_capability(tmp_path, "bad-cap", "BadCapability", BAD_CAPABILITY_CODE)
        # Should not raise, just log and skip
        count = await loader.load_all()
        assert count == 0

    @pytest.mark.asyncio
    async def test_load_one_nonexistent(self, loader):
        result = await loader.load_one("does-not-exist")
        assert result is False


# -- Unloading --


class TestUnloading:
    @pytest.mark.asyncio
    async def test_unload_all(self, loader, tmp_path):
        _write_capability(tmp_path, "cap-a", "SimpleCapability", SIMPLE_CAPABILITY_CODE)
        await loader.load_all()
        assert len(loader.get_capabilities()) == 1

        await loader.unload_all()
        assert len(loader.get_capabilities()) == 0
        assert len(loader.get_functions()) == 0

    @pytest.mark.asyncio
    async def test_unload_one(self, loader, tmp_path):
        _write_capability(tmp_path, "cap-a", "SimpleCapability", SIMPLE_CAPABILITY_CODE)
        _write_capability(tmp_path, "cap-b", "EmitCapability", EMIT_CAPABILITY_CODE)
        await loader.load_all()
        assert len(loader.get_capabilities()) == 2

        result = await loader.unload_one("cap-a")
        assert result is True
        assert len(loader.get_capabilities()) == 1
        assert "cap-b" in loader.get_capabilities()

    @pytest.mark.asyncio
    async def test_unload_nonexistent_returns_false(self, loader):
        result = await loader.unload_one("nonexistent")
        assert result is False


# -- Simulation mode --


class TestSimulationMode:
    @pytest.mark.asyncio
    async def test_simulation_mode_property(self, loader):
        assert loader.simulation_mode is False
        loader.simulation_mode = True
        assert loader.simulation_mode is True

    @pytest.mark.asyncio
    async def test_simulation_mode_tags_events(self, loader, tmp_path, event_emitter):
        loader.simulation_mode = True
        _write_capability(tmp_path, "emit-cap", "EmitCapability", EMIT_CAPABILITY_CODE)
        await loader.load_all()

        caps = loader.get_capabilities()
        instance = caps["emit-cap"].instance

        # Directly call _emit_event_internal (what @emit methods use)
        await instance._emit_event_internal("work_complete", {"result": "ok"})

        event_emitter.assert_awaited_once()
        call_args = event_emitter.call_args
        payload = call_args[0][1]
        assert payload["simulated"] is True


# -- Dependency checking --


class TestDependencyChecking:
    def test_check_dependencies_no_deps(self, loader):
        missing = loader._check_dependencies("test-cap", {})
        assert missing == []

    def test_check_dependencies_available(self, loader):
        manifest = {"dependencies": {"python": ["json", "os"]}}
        missing = loader._check_dependencies("test-cap", manifest)
        assert missing == []

    def test_check_dependencies_missing(self, loader):
        manifest = {"dependencies": {"python": ["nonexistent_package_xyz"]}}
        missing = loader._check_dependencies("test-cap", manifest)
        assert "nonexistent_package_xyz" in missing

    def test_check_dependencies_with_version_spec(self, loader):
        manifest = {"dependencies": {"python": ["json>=1.0.0"]}}
        missing = loader._check_dependencies("test-cap", manifest)
        assert missing == []
