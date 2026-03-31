"""Unit tests for device_connect_sdk.device module.

Tests DeviceRuntime lifecycle, build_rpc_response/build_rpc_error helpers,
and _D2DRouter — all with mocked messaging (no real NATS connection).
"""

import asyncio
import json
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from device_connect_sdk.device import DeviceRuntime, build_rpc_response, build_rpc_error, _D2DRouter
from device_connect_sdk.drivers import DeviceDriver, rpc, emit
from device_connect_sdk.types import DeviceCapabilities, DeviceIdentity, DeviceStatus


# ── Stub driver ───────────────────────────────────────────────────

class StubDriver(DeviceDriver):
    device_type = "stub"

    @property
    def identity(self):
        return DeviceIdentity(device_type="stub", manufacturer="Test")

    @property
    def status(self):
        return DeviceStatus(location="lab")

    @rpc()
    async def ping(self) -> dict:
        """Ping."""
        return {"pong": True}

    @emit()
    async def alert(self, level: str):
        """Alert."""
        pass

    async def connect(self):
        pass

    async def disconnect(self):
        pass


# ── build_rpc_response ────────────────────────────────────────────

class TestBuildRpcResponse:
    def test_returns_bytes(self):
        result = build_rpc_response("req-1", {"ok": True})
        assert isinstance(result, bytes)

    def test_valid_jsonrpc(self):
        result = build_rpc_response("req-1", {"ok": True})
        parsed = json.loads(result)
        assert parsed["jsonrpc"] == "2.0"
        assert parsed["id"] == "req-1"
        assert parsed["result"] == {"ok": True}

    def test_no_error_field(self):
        parsed = json.loads(build_rpc_response("r1", "done"))
        assert "error" not in parsed

    def test_null_result(self):
        parsed = json.loads(build_rpc_response("r2", None))
        assert parsed["result"] is None

    def test_list_result(self):
        parsed = json.loads(build_rpc_response("r3", [1, 2, 3]))
        assert parsed["result"] == [1, 2, 3]


# ── build_rpc_error ──────────────────────────────────────────────

class TestBuildRpcError:
    def test_returns_bytes(self):
        result = build_rpc_error("req-1", -32600, "Invalid Request")
        assert isinstance(result, bytes)

    def test_valid_jsonrpc_error(self):
        result = build_rpc_error("req-1", -32600, "Invalid Request")
        parsed = json.loads(result)
        assert parsed["jsonrpc"] == "2.0"
        assert parsed["id"] == "req-1"
        assert parsed["error"]["code"] == -32600
        assert parsed["error"]["message"] == "Invalid Request"

    def test_no_result_field(self):
        parsed = json.loads(build_rpc_error("e1", -32601, "Method not found"))
        assert "result" not in parsed

    def test_custom_error_code(self):
        parsed = json.loads(build_rpc_error("e2", -1, "Custom error"))
        assert parsed["error"]["code"] == -1
        assert parsed["error"]["message"] == "Custom error"


# ── _D2DRouter ───────────────────────────────────────────────────

class TestD2DRouter:
    def _make_router(self, messaging=None, tenant="default", timeout=30.0):
        messaging = messaging or AsyncMock()
        return _D2DRouter(messaging, tenant=tenant, timeout=timeout), messaging

    @pytest.mark.asyncio
    async def test_invoke_sends_correct_subject(self):
        router, messaging = self._make_router(tenant="acme")
        messaging.request = AsyncMock(return_value=json.dumps({"jsonrpc": "2.0", "id": "x", "result": {}}).encode())
        await router.invoke("cam-01", "capture")
        messaging.request.assert_called_once()
        subject = messaging.request.call_args[0][0]
        assert subject == "device-connect.acme.cam-01.cmd"

    @pytest.mark.asyncio
    async def test_invoke_sends_jsonrpc_payload(self):
        router, messaging = self._make_router()
        messaging.request = AsyncMock(return_value=json.dumps({"jsonrpc": "2.0", "id": "x", "result": {}}).encode())
        await router.invoke("cam-01", "capture", params={"res": "4k"})
        payload = json.loads(messaging.request.call_args[0][1])
        assert payload["jsonrpc"] == "2.0"
        assert payload["method"] == "capture"
        assert payload["params"] == {"res": "4k"}

    @pytest.mark.asyncio
    async def test_invoke_returns_parsed_response(self):
        router, messaging = self._make_router()
        messaging.request = AsyncMock(
            return_value=json.dumps({"jsonrpc": "2.0", "id": "x", "result": {"image": "abc"}}).encode()
        )
        resp = await router.invoke("cam-01", "capture")
        assert resp["result"] == {"image": "abc"}

    @pytest.mark.asyncio
    async def test_invoke_uses_custom_timeout(self):
        router, messaging = self._make_router(timeout=5.0)
        messaging.request = AsyncMock(return_value=json.dumps({"jsonrpc": "2.0", "id": "x", "result": {}}).encode())
        await router.invoke("cam-01", "capture", timeout=10.0)
        _, kwargs = messaging.request.call_args
        assert kwargs["timeout"] == 10.0

    @pytest.mark.asyncio
    async def test_publish_event_correct_subject(self):
        router, messaging = self._make_router(tenant="lab")
        messaging.publish = AsyncMock()
        await router.publish_event("arm-01", "plateGrasped", {"plate_id": "P1"})
        messaging.publish.assert_called_once()
        subject = messaging.publish.call_args[0][0]
        assert subject == "device-connect.lab.arm-01.event.plateGrasped"

    @pytest.mark.asyncio
    async def test_publish_event_strips_prefix(self):
        router, messaging = self._make_router()
        messaging.publish = AsyncMock()
        await router.publish_event("arm-01", "event/plateGrasped", {"plate_id": "P1"})
        subject = messaging.publish.call_args[0][0]
        assert subject == "device-connect.default.arm-01.event.plateGrasped"

    @pytest.mark.asyncio
    async def test_publish_event_payload(self):
        router, messaging = self._make_router()
        messaging.publish = AsyncMock()
        await router.publish_event("arm-01", "done", {"ok": True})
        payload = json.loads(messaging.publish.call_args[0][1])
        assert payload["jsonrpc"] == "2.0"
        assert payload["method"] == "done"
        assert payload["params"] == {"ok": True}


# ── DeviceRuntime.__init__ ───────────────────────────────────────

class TestDeviceRuntimeInit:
    def test_with_driver(self):
        driver = StubDriver()
        rt = DeviceRuntime(driver=driver, device_id="dev-1", messaging_urls=["nats://localhost:4222"])
        assert rt.device_id == "dev-1"
        assert rt._driver is driver

    def test_with_capability_dicts(self):
        caps = DeviceCapabilities(description="test", functions=[], events=[])
        ident = DeviceIdentity(device_type="sensor", manufacturer="Acme")
        status = DeviceStatus(location="room-1")
        rt = DeviceRuntime(
            capabilities=caps,
            identity=ident,
            status=status,
            device_id="sensor-1",
            messaging_urls=["nats://localhost:4222"],
        )
        assert rt.device_id == "sensor-1"
        assert rt.capabilities.description == "test"
        assert rt.identity["device_type"] == "sensor"
        assert rt.status["location"] == "room-1"

    def test_with_dict_capabilities(self):
        rt = DeviceRuntime(
            capabilities={"description": "dict-caps", "functions": [], "events": []},
            identity={"device_type": "sensor"},
            status={"location": "lab"},
            device_id="s-1",
            messaging_urls=["nats://localhost:4222"],
        )
        assert rt.capabilities.description == "dict-caps"

    def test_default_device_id_generated(self):
        rt = DeviceRuntime(messaging_urls=["nats://localhost:4222"])
        assert rt.device_id.startswith("device-")
        assert len(rt.device_id) == len("device-") + 8

    def test_default_tenant(self):
        rt = DeviceRuntime(device_id="d", messaging_urls=["nats://localhost:4222"])
        assert rt.tenant == "default"

    def test_custom_tenant(self):
        rt = DeviceRuntime(device_id="d", tenant="acme", messaging_urls=["nats://localhost:4222"])
        assert rt.tenant == "acme"

    def test_default_ttl(self):
        rt = DeviceRuntime(device_id="d", messaging_urls=["nats://localhost:4222"])
        assert rt.ttl == 15

    def test_heartbeat_interval_default(self):
        rt = DeviceRuntime(device_id="d", messaging_urls=["nats://localhost:4222"])
        assert rt._heartbeat_interval == 15 / 3  # ttl / 3

    def test_heartbeat_interval_custom(self):
        rt = DeviceRuntime(device_id="d", messaging_urls=["nats://localhost:4222"], heartbeat_interval=2.0)
        assert rt._heartbeat_interval == 2.0

    def test_heartbeat_interval_minimum_one(self):
        rt = DeviceRuntime(device_id="d", messaging_urls=["nats://localhost:4222"], ttl=2)
        # max(1.0, 2/3) == 1.0
        assert rt._heartbeat_interval == 1.0

    def test_messaging_urls_from_param(self):
        rt = DeviceRuntime(device_id="d", messaging_urls=["nats://host1:4222"])
        assert rt.messaging_urls == ["nats://host1:4222"]

    def test_messaging_urls_from_env(self):
        with patch.dict(os.environ, {"MESSAGING_URLS": "nats://env1:4222,nats://env2:4222"}, clear=False):
            rt = DeviceRuntime(device_id="d")
            assert rt.messaging_urls == ["nats://env1:4222", "nats://env2:4222"]

    def test_messaging_urls_from_nats_url_env(self):
        with patch.dict(os.environ, {"NATS_URL": "nats://nats-env:4222"}, clear=False):
            # Clear MESSAGING_URLS to ensure fallback
            env = {k: v for k, v in os.environ.items() if k != "MESSAGING_URLS"}
            with patch.dict(os.environ, env, clear=True):
                rt = DeviceRuntime(device_id="d")
                assert rt.messaging_urls == ["nats://nats-env:4222"]

    def test_no_urls_enters_d2d_mode(self):
        with patch.dict(os.environ, {}, clear=True):
            rt = DeviceRuntime(device_id="d")
            assert rt._d2d_mode is True
            assert rt._messaging_backend == "zenoh"
            assert rt.messaging_urls == []

    def test_auto_detect_nats_backend(self):
        rt = DeviceRuntime(device_id="d", messaging_urls=["nats://localhost:4222"])
        assert rt._messaging_backend == "nats"

    def test_auto_detect_mqtt_backend(self):
        rt = DeviceRuntime(device_id="d", messaging_urls=["mqtt://localhost:1883"])
        assert rt._messaging_backend == "mqtt"

    def test_auto_detect_tls_as_nats(self):
        rt = DeviceRuntime(device_id="d", messaging_urls=["tls://localhost:4222"])
        assert rt._messaging_backend == "nats"

    def test_auto_detect_mqtts_as_mqtt(self):
        rt = DeviceRuntime(device_id="d", messaging_urls=["mqtts://localhost:8883"])
        assert rt._messaging_backend == "mqtt"

    def test_explicit_backend_overrides_autodetect(self):
        rt = DeviceRuntime(
            device_id="d",
            messaging_urls=["nats://localhost:4222"],
            messaging_backend="mqtt",
        )
        assert rt._messaging_backend == "mqtt"


# ── DeviceRuntime.__init__ with driver ───────────────────────────

class TestDeviceRuntimeInitWithDriver:
    def test_driver_set_device_called(self):
        driver = StubDriver()
        rt = DeviceRuntime(driver=driver, device_id="d1", messaging_urls=["nats://localhost:4222"])
        assert driver._device is rt

    def test_capabilities_from_driver(self):
        driver = StubDriver()
        rt = DeviceRuntime(driver=driver, device_id="d1", messaging_urls=["nats://localhost:4222"])
        func_names = [f.name for f in rt.capabilities.functions]
        assert "ping" in func_names

    def test_events_from_driver(self):
        driver = StubDriver()
        rt = DeviceRuntime(driver=driver, device_id="d1", messaging_urls=["nats://localhost:4222"])
        event_names = [e.name for e in rt.capabilities.events]
        assert "alert" in event_names

    def test_identity_from_driver(self):
        driver = StubDriver()
        rt = DeviceRuntime(driver=driver, device_id="d1", messaging_urls=["nats://localhost:4222"])
        assert rt.identity["device_type"] == "stub"
        assert rt.identity["manufacturer"] == "Test"

    def test_status_from_driver(self):
        driver = StubDriver()
        rt = DeviceRuntime(driver=driver, device_id="d1", messaging_urls=["nats://localhost:4222"])
        assert rt.status["location"] == "lab"

    def test_explicit_identity_overrides_driver(self):
        driver = StubDriver()
        rt = DeviceRuntime(
            driver=driver,
            identity=DeviceIdentity(device_type="stub", manufacturer="Override"),
            device_id="d1",
            messaging_urls=["nats://localhost:4222"],
        )
        assert rt.identity["manufacturer"] == "Override"


# ── enqueue_event ────────────────────────────────────────────────

class TestEnqueueEvent:
    @pytest.mark.asyncio
    async def test_enqueue_puts_on_queue(self):
        rt = DeviceRuntime(device_id="dev-1", tenant="lab", messaging_urls=["nats://localhost:4222"])
        await rt.enqueue_event("readComplete", {"plate_id": "P1"})
        assert rt._event_queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_enqueue_correct_subject(self):
        rt = DeviceRuntime(device_id="dev-1", tenant="lab", messaging_urls=["nats://localhost:4222"])
        await rt.enqueue_event("readComplete", {"plate_id": "P1"})
        subject, data = await rt._event_queue.get()
        assert subject == "device-connect.lab.dev-1.event.readComplete"

    @pytest.mark.asyncio
    async def test_enqueue_correct_payload(self):
        rt = DeviceRuntime(device_id="dev-1", tenant="lab", messaging_urls=["nats://localhost:4222"])
        await rt.enqueue_event("readComplete", {"plate_id": "P1"})
        subject, data = await rt._event_queue.get()
        parsed = json.loads(data)
        assert parsed["jsonrpc"] == "2.0"
        assert parsed["method"] == "readComplete"
        assert parsed["params"] == {"plate_id": "P1"}

    @pytest.mark.asyncio
    async def test_enqueue_multiple_events(self):
        rt = DeviceRuntime(device_id="dev-1", messaging_urls=["nats://localhost:4222"])
        await rt.enqueue_event("eventA", {"a": 1})
        await rt.enqueue_event("eventB", {"b": 2})
        assert rt._event_queue.qsize() == 2


def _fake_package(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []  # type: ignore[attr-defined]
    return module


# ── Split-package compatibility regressions ──────────────────────

class TestSplitPackageImports:
    def test_missing_credentials_error_references_devctl_module(self):
        rt = DeviceRuntime(device_id="sensor-1", messaging_urls=["nats://localhost:4222"])

        with pytest.raises(FileNotFoundError) as excinfo:
            rt._load_credentials("/definitely/missing.creds.json")

        assert "python -m device_connect_server.devctl commission" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_run_commissioning_imports_from_device_connect_server(self, tmp_path):
        calls = {}

        class FakeCommissioningMode:
            def __init__(
                self,
                *,
                device_id,
                device_type,
                factory_pin,
                capabilities,
                nkey_public,
                nkey_seed,
                port,
            ):
                calls["init"] = {
                    "device_id": device_id,
                    "device_type": device_type,
                    "factory_pin": factory_pin,
                    "capabilities": capabilities,
                    "nkey_public": nkey_public,
                    "nkey_seed": nkey_seed,
                    "port": port,
                }

            async def start_commissioning_server(self):
                calls["started"] = True
                return {"device_id": "sensor-1", "nats": {"jwt": "jwt", "nkey_seed": "seed"}}

            def save_credentials(self, credentials, path):
                calls["saved"] = {"credentials": credentials, "path": path}

        server_pkg = _fake_package("device_connect_server")
        security_pkg = _fake_package("device_connect_server.security")
        commissioning_mod = types.ModuleType("device_connect_server.security.commissioning")
        commissioning_mod.CommissioningMode = FakeCommissioningMode

        identity_payload = {
            "device_id": "sensor-1",
            "device_type": "sensor",
            "capabilities": {"functions": [], "events": []},
            "provisioning": {"pin": "1234-5678", "commissioned": False},
            "nkey": {"public_key": "PUB", "seed": "SEED"},
        }
        identity_path = tmp_path / "factory_identity.json"
        identity_path.write_text(json.dumps(identity_payload))

        rt = DeviceRuntime(
            device_id="sensor-1",
            messaging_urls=["nats://localhost:4222"],
            factory_identity_file=str(identity_path),
            auto_commission=False,
        )
        rt._factory_identity = dict(identity_payload)

        with patch.dict(
            sys.modules,
            {
                "device_connect_server": server_pkg,
                "device_connect_server.security": security_pkg,
                "device_connect_server.security.commissioning": commissioning_mod,
            },
            clear=False,
        ):
            creds_path = await rt._run_commissioning()

        assert calls["init"]["device_id"] == "sensor-1"
        assert calls["init"]["device_type"] == "sensor"
        assert calls["init"]["port"] == rt.commissioning_port
        assert calls["started"] is True
        assert calls["saved"]["path"] == creds_path
        assert rt._factory_identity["provisioning"]["commissioned"] is True

    @pytest.mark.asyncio
    async def test_setup_agentic_driver_imports_registry_from_device_connect_server(self):
        class FakeRegistryClient:
            def __init__(self, messaging, config, tenant="default"):
                self.messaging = messaging
                self.config = config
                self.tenant = tenant

        server_pkg = _fake_package("device_connect_server")
        registry_pkg = _fake_package("device_connect_server.registry")
        registry_mod = types.ModuleType("device_connect_server.registry.client")
        registry_mod.RegistryClient = FakeRegistryClient

        driver = StubDriver()
        rt = DeviceRuntime(
            driver=driver,
            device_id="sensor-1",
            messaging_urls=["nats://localhost:4222"],
        )
        rt._d2d_mode = False
        rt.messaging = AsyncMock()

        with patch.dict(
            sys.modules,
            {
                "device_connect_server": server_pkg,
                "device_connect_server.registry": registry_pkg,
                "device_connect_server.registry.client": registry_mod,
            },
            clear=False,
        ):
            await rt._setup_agentic_driver()

        assert isinstance(driver.registry, FakeRegistryClient)
        assert driver.registry.messaging is rt.messaging
        assert driver.registry.tenant == "default"
        assert driver.registry.config.backend == "nats"
        assert driver.registry.config.servers == ["nats://localhost:4222"]
