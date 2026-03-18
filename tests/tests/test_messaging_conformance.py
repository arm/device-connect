"""Messaging backend conformance tests.

Validates that each MessagingClient implementation satisfies the ABC contract.
Parameterized over backends: NATS and Zenoh.

Run all:    pytest tests/test_messaging_conformance.py -v
Run NATS:   pytest tests/test_messaging_conformance.py -v -k nats
Run Zenoh:  pytest tests/test_messaging_conformance.py -v -k zenoh
"""

import asyncio
import json
import os
import uuid

import pytest

# Import the ABC — this comes from whichever package provides it
# (device-connect-server or device-connect-sdk both have messaging/)
from device_connect_sdk.messaging.base import MessagingClient


# ── Client factory ─────────────────────────────────────────────────

def _create_nats_client() -> MessagingClient:
    """Create a NATS MessagingClient instance."""
    from device_connect_sdk.messaging.nats_adapter import NATSAdapter
    return NATSAdapter()


def _create_zenoh_client() -> MessagingClient:
    """Create a Zenoh MessagingClient instance."""
    pytest.importorskip("zenoh", reason="eclipse-zenoh not installed")
    from device_connect_sdk.messaging.zenoh_adapter import ZenohAdapter
    return ZenohAdapter()


BACKEND_FACTORIES = {
    "nats": _create_nats_client,
    "zenoh": _create_zenoh_client,
    # "mqtt": _create_mqtt_client,  # Add when MQTT broker is in docker-compose
}

BACKEND_SERVERS = {
    "nats": [os.getenv("NATS_URL", "nats://localhost:4222")],
    "zenoh": [os.getenv("ZENOH_CONNECT", "tcp/localhost:7447")],
    # "mqtt": ["mqtt://localhost:1883"],
}


@pytest.fixture(params=["nats", "zenoh"])
def backend_name(request) -> str:
    return request.param


@pytest.fixture
async def messaging_client(backend_name, infrastructure):
    """Create and connect a MessagingClient for the given backend."""
    factory = BACKEND_FACTORIES[backend_name]
    client = factory()
    servers = BACKEND_SERVERS[backend_name]

    await client.connect(servers=servers)
    try:
        yield client
    finally:
        await client.close()


# ── Conformance tests ──────────────────────────────────────────────

class TestMessagingConformance:
    """Tests every MessagingClient implementation must pass."""

    @pytest.mark.asyncio
    @pytest.mark.conformance
    async def test_connect_and_is_connected(self, messaging_client):
        """After connect(), is_connected should be True."""
        assert messaging_client.is_connected is True
        assert messaging_client.is_closed is False

    @pytest.mark.asyncio
    @pytest.mark.conformance
    async def test_close(self, backend_name, infrastructure):
        """After close(), is_closed should be True."""
        factory = BACKEND_FACTORIES[backend_name]
        client = factory()
        await client.connect(servers=BACKEND_SERVERS[backend_name])
        assert client.is_connected is True

        await client.close()
        assert client.is_closed is True

    @pytest.mark.asyncio
    @pytest.mark.conformance
    async def test_publish_subscribe(self, messaging_client):
        """publish() + subscribe() should deliver messages."""
        received = []
        subject = f"conformance.test.{uuid.uuid4().hex[:6]}"

        async def handler(data, reply):
            received.append(json.loads(data))

        sub = await messaging_client.subscribe(subject, handler)
        try:
            payload = {"test": "publish_subscribe", "value": 42}
            await messaging_client.publish(subject, json.dumps(payload).encode())

            for _ in range(50):
                if received:
                    break
                await asyncio.sleep(0.05)

            assert len(received) == 1
            assert received[0]["value"] == 42
        finally:
            await sub.unsubscribe()

    @pytest.mark.asyncio
    @pytest.mark.conformance
    async def test_request_reply(self, messaging_client):
        """request() should get a reply from a subscriber."""
        subject = f"conformance.rpc.{uuid.uuid4().hex[:6]}"

        async def handler(data, reply):
            if reply:
                response = {"echo": json.loads(data)}
                await messaging_client.publish(reply, json.dumps(response).encode())

        sub = await messaging_client.subscribe(subject, handler)
        try:
            request_data = {"method": "ping", "params": {"ts": 123}}
            reply = await messaging_client.request(
                subject, json.dumps(request_data).encode(), timeout=5.0,
            )
            response = json.loads(reply)
            assert response["echo"]["method"] == "ping"
        finally:
            await sub.unsubscribe()

    @pytest.mark.asyncio
    @pytest.mark.conformance
    async def test_wildcard_subscribe(self, messaging_client):
        """Wildcard subscribe (*.event.*) should match multiple subjects."""
        received = []
        pattern = f"conformance.*.event.{uuid.uuid4().hex[:6]}"
        unique = uuid.uuid4().hex[:6]

        async def handler(data, reply):
            received.append(json.loads(data))

        sub = await messaging_client.subscribe(
            f"conformance.*.event.{unique}", handler,
        )
        try:
            await messaging_client.publish(
                f"conformance.deviceA.event.{unique}",
                json.dumps({"source": "A"}).encode(),
            )
            await messaging_client.publish(
                f"conformance.deviceB.event.{unique}",
                json.dumps({"source": "B"}).encode(),
            )

            for _ in range(50):
                if len(received) >= 2:
                    break
                await asyncio.sleep(0.05)

            assert len(received) == 2
            sources = {r["source"] for r in received}
            assert sources == {"A", "B"}
        finally:
            await sub.unsubscribe()

    @pytest.mark.asyncio
    @pytest.mark.conformance
    async def test_unsubscribe(self, messaging_client):
        """After unsubscribe, no more messages should be received."""
        received = []
        subject = f"conformance.unsub.{uuid.uuid4().hex[:6]}"

        async def handler(data, reply):
            received.append(True)

        sub = await messaging_client.subscribe(subject, handler)

        # Send message — should be received
        await messaging_client.publish(subject, b'{"before": true}')
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.05)
        assert len(received) == 1

        # Unsubscribe
        await sub.unsubscribe()

        # Send another — should NOT be received
        received.clear()
        await messaging_client.publish(subject, b'{"after": true}')
        await asyncio.sleep(0.5)
        assert len(received) == 0

    @pytest.mark.asyncio
    @pytest.mark.conformance
    async def test_subject_syntax_conversion(self, messaging_client):
        """convert_subject_syntax should return valid subject for each backend."""
        result = messaging_client.convert_subject_syntax("device-connect.tenant.device.event.alert")
        # NATS: unchanged (dots); Zenoh: dots→slashes; MQTT: dots→slashes
        assert isinstance(result, str)
        assert len(result) > 0
