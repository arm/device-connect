"""Integration tests for D2D device discovery (no Docker infrastructure).

Tests that devices can discover each other and communicate via Zenoh
device-to-device multicast scouting — no router, no etcd, no registry.

Run WITHOUT Docker:
    python3 -m pytest tests/test_d2d_discovery.py -v

Requires: eclipse-zenoh Python package installed.
"""

import asyncio
import json

import pytest

# Skip entire module if zenoh is not installed
zenoh = pytest.importorskip("zenoh", reason="eclipse-zenoh not installed")

pytestmark = [
    pytest.mark.d2d,
]


@pytest.fixture(autouse=True)
def d2d_env(monkeypatch):
    """Set environment for D2D mode — no broker URLs."""
    monkeypatch.setenv("MESSAGING_BACKEND", "zenoh")
    monkeypatch.setenv("DEVICE_CONNECT_ALLOW_INSECURE", "true")
    monkeypatch.setenv("DEVICE_CONNECT_DISCOVERY_MODE", "d2d")
    # Remove any broker URLs to ensure pure D2D mode
    monkeypatch.delenv("ZENOH_CONNECT", raising=False)
    monkeypatch.delenv("MESSAGING_URLS", raising=False)
    monkeypatch.delenv("NATS_URL", raising=False)
    monkeypatch.delenv("NATS_URLS", raising=False)


from device_connect_sdk.drivers import DeviceDriver, rpc  # noqa: E402
from device_connect_sdk.types import DeviceIdentity, DeviceStatus  # noqa: E402


class _StubDriver(DeviceDriver):
    """Minimal driver for D2D integration tests."""

    def __init__(self, dt="sensor"):
        super().__init__()
        self._dt = dt

    @property
    def device_type(self):
        return self._dt

    @property
    def identity(self):
        return DeviceIdentity(device_type=self._dt, manufacturer="Test")

    @property
    def status(self):
        return DeviceStatus(availability="available", location="lab")

    @rpc()
    async def ping(self) -> dict:
        """Ping test."""
        return {"pong": True, "device_type": self._dt}

    async def connect(self):
        pass

    async def disconnect(self):
        pass


async def _start_device(device_id, device_type="sensor"):
    """Start a DeviceRuntime in D2D mode, return (runtime, task)."""
    from device_connect_sdk import DeviceRuntime

    driver = _StubDriver(dt=device_type)
    runtime = DeviceRuntime(
        driver=driver,
        device_id=device_id,
        messaging_backend="zenoh",
        allow_insecure=True,
    )
    task = asyncio.create_task(runtime.run())
    return runtime, task


async def _stop_device(runtime, task):
    """Stop a device runtime."""
    try:
        await runtime.stop()
    except Exception:
        pass
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_two_devices_discover_each_other():
    """Two devices in D2D mode should discover each other via presence."""
    runtime_a, task_a = await _start_device("d2d-sensor-a", "sensor")
    runtime_b, task_b = await _start_device("d2d-robot-b", "robot")

    try:
        # Wait for presence announcements to propagate
        await asyncio.sleep(5)

        # Device A should see Device B via its D2D collector
        assert runtime_a._d2d_collector is not None, "Device A should have a D2D collector"
        peers_a = await runtime_a._d2d_collector.list_devices()
        peer_ids_a = [p["device_id"] for p in peers_a]
        assert "d2d-robot-b" in peer_ids_a, f"Device A didn't discover B. Peers: {peer_ids_a}"

        # Device B should see Device A
        assert runtime_b._d2d_collector is not None, "Device B should have a D2D collector"
        peers_b = await runtime_b._d2d_collector.list_devices()
        peer_ids_b = [p["device_id"] for p in peers_b]
        assert "d2d-sensor-a" in peer_ids_b, f"Device B didn't discover A. Peers: {peer_ids_b}"
    finally:
        await _stop_device(runtime_a, task_a)
        await _stop_device(runtime_b, task_b)


@pytest.mark.asyncio
async def test_d2d_rpc_between_devices():
    """Device A can call Device B's RPC in D2D mode (no registry)."""
    runtime_a, task_a = await _start_device("d2d-caller", "robot")
    runtime_b, task_b = await _start_device("d2d-responder", "sensor")

    try:
        await asyncio.sleep(5)

        # Send RPC from A to B using raw messaging (same as _D2DRouter does)
        request = {
            "jsonrpc": "2.0",
            "id": "d2d-rpc-1",
            "method": "ping",
            "params": {},
        }
        response_data = await runtime_a.messaging.request(
            "device-connect.default.d2d-responder.cmd",
            json.dumps(request).encode(),
            timeout=5.0,
        )
        response = json.loads(response_data)
        assert "result" in response, f"RPC failed: {response}"
        assert response["result"]["pong"] is True
        assert response["result"]["device_type"] == "sensor"
    finally:
        await _stop_device(runtime_a, task_a)
        await _stop_device(runtime_b, task_b)


@pytest.mark.asyncio
async def test_discover_devices_d2d_via_tools():
    """device-connect-agent-tools discover_devices() works in D2D mode."""
    runtime_a, task_a = await _start_device("d2d-tools-sensor", "sensor")

    try:
        await asyncio.sleep(5)

        from device_connect_agent_tools.connection import _DeviceConnectConnection

        conn = _DeviceConnectConnection(zone="default")
        conn.connect()
        try:
            # The tools connection creates a separate Zenoh session that needs
            # time to discover the device's session via multicast scouting.
            # Retry a few times to give scouting time to connect the sessions.
            device_ids = []
            for attempt in range(5):
                devices = conn.list_devices()
                device_ids = [d.get("device_id") for d in devices]
                if "d2d-tools-sensor" in device_ids:
                    break
                await asyncio.sleep(2)

            assert "d2d-tools-sensor" in device_ids, (
                f"Expected d2d-tools-sensor in {device_ids}"
            )
        finally:
            conn.close()
    finally:
        await _stop_device(runtime_a, task_a)


@pytest.mark.asyncio
async def test_d2d_mode_skips_registration():
    """In D2D mode, device should not attempt registry registration."""
    runtime, task = await _start_device("d2d-no-reg", "sensor")

    try:
        await asyncio.sleep(2)

        # Device should be running
        assert runtime._d2d_mode is True, "Should be in D2D mode"
        # Announcer should be active
        assert runtime._d2d_announcer is not None, "Announcer should be started"
        assert runtime._d2d_announcer._task is not None, "Announcer task should be running"
        # Registration ID should NOT be set (no registry)
        assert not getattr(runtime, '_registration_id', None), (
            "Should not have a registration ID in D2D mode"
        )
    finally:
        await _stop_device(runtime, task)
