"""Integration tests for device-connect-agent-tools.

No dependency on device-connect-server — tests the standalone package.

Requires the full Device Connect stack running:
    cd /path/to/Fab/core
    docker compose up nats-jwt etcd device-registry-service -d

Run:
    cd /Users/kavche01/Fab
    PYTHONPATH="device-connect-agent-tools" pytest device-connect-agent-tools/tests/ -v --timeout=120
"""

import asyncio
import json
import os
import time
import uuid

import pytest


# ═══════════════════════════════════════════════════════════════════
# 1. Connection — connect/disconnect/auto-connect
# ═══════════════════════════════════════════════════════════════════

class TestConnection:
    """Test the NATS connection singleton."""

    def test_connect_disconnect(self, device_connect_connection):
        """connect() and disconnect() should work without error."""
        from device_connect_agent_tools.connection import get_connection

        conn = get_connection()
        assert conn is not None
        assert conn.messaging_client is not None
        assert not conn.messaging_client.is_closed

    def test_connect_is_idempotent(self, device_connect_connection):
        """Calling connect() twice should not error or create a second connection."""
        from device_connect_agent_tools import connect, get_connection

        conn1 = get_connection()
        connect()  # second call — no-op
        conn2 = get_connection()
        assert conn1 is conn2


# ═══════════════════════════════════════════════════════════════════
# 2. Tools — discover_devices against live NATS + real registry
# ═══════════════════════════════════════════════════════════════════

class TestToolsWithNats:
    """Test @tool functions against live NATS."""

    def test_discover_devices_returns_list(self, device_connect_connection):
        """discover_devices() should return a list (possibly empty)."""
        from device_connect_agent_tools import discover_devices

        result = discover_devices()
        assert isinstance(result, list)

    def test_discover_devices_with_filter(self, device_connect_connection):
        """Filtering by device_type should still return a list."""
        from device_connect_agent_tools import discover_devices

        result = discover_devices(device_type="camera")
        assert isinstance(result, list)

    def test_invoke_device_unknown(self, device_connect_connection):
        """invoke_device() on a non-existent device should return success=False."""
        from device_connect_agent_tools import invoke_device

        result = invoke_device(
            device_id="nonexistent-xyz",
            function="ping",
            llm_reasoning="Testing error handling",
        )
        assert isinstance(result, dict)
        assert result.get("success") is False

    def test_get_device_status_unknown(self, device_connect_connection):
        """get_device_status() for unknown device should return error dict."""
        from device_connect_agent_tools import get_device_status

        result = get_device_status(device_id="nonexistent-xyz")
        assert isinstance(result, dict)
        assert "error" in result or "device_id" in result


# ═══════════════════════════════════════════════════════════════════
# 3. DeviceConnectAgent — prepare discovers devices
# ═══════════════════════════════════════════════════════════════════

class TestDeviceConnectAgent:
    """Test DeviceConnectAgent lifecycle against live NATS."""

    @pytest.mark.asyncio
    async def test_prepare_connects_and_discovers(self, device_connect_connection):
        """DeviceConnectAgent.prepare() should connect and discover devices."""
        from device_connect_agent_tools import DeviceConnectAgent, disconnect

        # Disconnect module singleton so DeviceConnectAgent can create its own
        disconnect()

        agent = DeviceConnectAgent(goal="test agent", **device_connect_connection)
        try:
            info = await agent.prepare()
            assert "devices" in info
            assert "goal" in info
            assert info["goal"] == "test agent"
            assert isinstance(info["devices"], list)
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_context_manager(self, device_connect_connection):
        """DeviceConnectAgent should work as async context manager."""
        from device_connect_agent_tools import DeviceConnectAgent, disconnect

        disconnect()

        async with DeviceConnectAgent(goal="test", **device_connect_connection) as agent:
            assert agent.devices is not None


# ═══════════════════════════════════════════════════════════════════
# 4. DeviceConnectAgent — event reception
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.integration
class TestDeviceConnectAgentEvents:
    """Test that DeviceConnectAgent receives NATS events."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_event_received(self, nats_client, device_connect_connection):
        """DeviceConnectAgent should receive events published to NATS."""
        from device_connect_agent_tools import DeviceConnectAgent, disconnect

        disconnect()

        received_events = []

        def on_event(device_id, event_name, params):
            received_events.append((device_id, event_name, params))

        agent = DeviceConnectAgent(
            goal="test event reception",
            on_event=on_event,
            batch_window=0.1,
            **device_connect_connection,
        )

        # Skip LLM processing
        agent._run_agent_sync = lambda prompt: "ok"

        run_task = None
        try:
            await agent.prepare()
            run_task = asyncio.create_task(agent.run())
            await asyncio.sleep(0.5)

            # Publish a fake event
            event_payload = {
                "jsonrpc": "2.0",
                "method": "motion_detected",
                "params": {"zone": "A", "confidence": 0.95},
            }
            await nats_client.publish(
                f"device-connect.{agent.zone}.test-cam-001.event.motion_detected",
                json.dumps(event_payload).encode(),
            )
            await nats_client.flush()

            # Wait for event
            for _ in range(30):
                if received_events:
                    break
                await asyncio.sleep(0.1)

            assert len(received_events) >= 1
            device_id, event_name, params = received_events[0]
            assert device_id == "test-cam-001"
            assert event_name == "motion_detected"
            assert params["zone"] == "A"

        finally:
            await agent.stop()
            if run_task is not None:
                run_task.cancel()
                try:
                    await run_task
                except asyncio.CancelledError:
                    pass


# ═══════════════════════════════════════════════════════════════════
# 5. Standalone tools with LLM agent
# ═══════════════════════════════════════════════════════════════════

class TestStandaloneToolsLLM:
    """Test using Device Connect tools directly with a Strands Agent."""

    @pytest.mark.timeout(60)
    def test_strands_agent_with_device_connect_tools(self, device_connect_connection, api_key):
        """A plain Strands Agent should be able to use Device Connect tools."""
        from strands import Agent
        from device_connect_agent_tools import discover_devices, invoke_device

        api_key_value, provider = api_key

        if provider == "anthropic":
            os.environ["ANTHROPIC_API_KEY"] = api_key_value
            from strands.models import AnthropicModel
            model = AnthropicModel(model_id="claude-sonnet-4-20250514", max_tokens=4096)
        else:
            os.environ["OPENAI_API_KEY"] = api_key_value
            from strands.models import OpenAIModel
            model = OpenAIModel(model_id="gpt-4o")

        agent = Agent(
            model=model,
            tools=[discover_devices],
            system_prompt="You help discover devices. When asked, call discover_devices().",
        )
        response = agent("What devices are available? Use discover_devices to find out.")

        assert response is not None
        assert len(str(response)) > 0


# ═══════════════════════════════════════════════════════════════════
# 6. Simulated device — register via real registry, command handling,
#    event emission (all against real Device Connect stack).
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.integration
class TestSimulatedDevice:
    """Test simulated devices against the real Device Connect registry + NATS."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_device_registers_and_discoverable(self, nats_client, device_connect_connection):
        """A device registered via JSON-RPC should be found by discover_devices()."""
        device_id = f"test-sim-{uuid.uuid4().hex[:6]}"
        zone = "default"

        # Register with the real registry service
        reg_payload = {
            "jsonrpc": "2.0",
            "id": f"{device_id}-{int(time.time() * 1000)}",
            "method": "registerDevice",
            "params": {
                "device_id": device_id,
                "device_ttl": 30,
                "capabilities": {
                    "description": "Test simulated camera",
                    "functions": [
                        {"name": "capture_image", "description": "Capture image", "parameters": {}},
                    ],
                    "events": [
                        {"name": "motion_detected", "description": "Motion detected", "parameters": {}},
                    ],
                },
                "identity": {
                    "device_type": "camera",
                    "manufacturer": "Test",
                    "model": "SimCam-1",
                    "firmware_version": "0.1.0",
                },
                "status": {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "location": "test-lab",
                    "availability": "idle",
                    "online": True,
                },
            },
        }

        msg = await nats_client.request(
            f"device-connect.{zone}.registry",
            json.dumps(reg_payload).encode(),
            timeout=5.0,
        )
        response = json.loads(msg.decode())
        assert "result" in response, f"Registration failed: {response}"
        assert response["result"]["status"] == "registered"

        # discover_devices should find it
        from device_connect_agent_tools import discover_devices

        devices = discover_devices()
        device_ids = [d["device_id"] for d in devices]
        assert device_id in device_ids, f"{device_id} not found in {device_ids}"

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_device_connect_agent_receives_events(self, nats_client, device_connect_connection):
        """DeviceConnectAgent should receive events from a registered device."""
        from device_connect_agent_tools import DeviceConnectAgent, disconnect

        disconnect()

        device_id = f"test-evt-cam-{uuid.uuid4().hex[:6]}"
        zone = "default"

        # Register the device with the real registry
        reg_payload = {
            "jsonrpc": "2.0",
            "id": f"{device_id}-reg",
            "method": "registerDevice",
            "params": {
                "device_id": device_id,
                "device_ttl": 30,
                "capabilities": {
                    "description": "Event test camera",
                    "functions": [{"name": "capture_image", "description": "Capture", "parameters": {}}],
                    "events": [{"name": "motion_detected", "description": "Motion", "parameters": {}}],
                },
                "identity": {"device_type": "camera", "manufacturer": "Test", "model": "EvtCam", "firmware_version": "0.1"},
                "status": {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "location": "lab", "online": True},
            },
        }
        await nats_client.request(f"device-connect.{zone}.registry", json.dumps(reg_payload).encode(), timeout=5.0)

        # Start DeviceConnectAgent with event tracking
        received_events = []

        def on_event(dev_id, event_name, params):
            received_events.append((dev_id, event_name, params))

        agent = DeviceConnectAgent(
            goal="test event reception from registered device",
            on_event=on_event,
            batch_window=0.1,
            **device_connect_connection,
        )
        agent._run_agent_sync = lambda prompt: "acknowledged"

        run_task = None
        try:
            await agent.prepare()

            # Verify the device was discovered
            cam_ids = [d["device_id"] for d in agent.devices]
            assert device_id in cam_ids, f"Device not discovered: {cam_ids}"

            run_task = asyncio.create_task(agent.run())
            await asyncio.sleep(0.5)

            # Emit a motion event
            event_payload = {
                "jsonrpc": "2.0",
                "method": "motion_detected",
                "params": {
                    "zone": "warehouse-B",
                    "confidence": 0.92,
                    "object_type": "person",
                    "device_id": device_id,
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            }
            await nats_client.publish(
                f"device-connect.{zone}.{device_id}.event.motion_detected",
                json.dumps(event_payload).encode(),
            )
            await nats_client.flush()

            # Wait for the event
            for _ in range(30):
                if received_events:
                    break
                await asyncio.sleep(0.1)

            assert len(received_events) >= 1, "No events received by DeviceConnectAgent"
            dev_id, evt_name, params = received_events[0]
            assert dev_id == device_id
            assert evt_name == "motion_detected"
            assert params["zone"] == "warehouse-B"
            assert params["confidence"] == 0.92

        finally:
            await agent.stop()
            if run_task is not None:
                run_task.cancel()
                try:
                    await run_task
                except asyncio.CancelledError:
                    pass
