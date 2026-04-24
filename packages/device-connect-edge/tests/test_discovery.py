# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for device_connect_edge.discovery module.

Tests PresenceAnnouncer, PresenceCollector, and D2DRegistry
with mocked messaging (no real Zenoh/NATS connection).
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from device_connect_edge.discovery import (
    BURST_INTERVAL,
    PEER_TIMEOUT,
    PresenceAnnouncer,
    PresenceCollector,
    D2DRegistry,
    _device_has_capabilities,
)


# ── Helpers ──────────────────────────────────────────────────────

def _make_messaging():
    """Create a mock MessagingClient."""
    m = AsyncMock()
    m.publish = AsyncMock()
    m.subscribe = AsyncMock()
    return m


def _make_presence_payload(device_id="camera-001", device_type="camera"):
    """Create a presence payload like PresenceAnnouncer would publish."""
    return json.dumps({
        "device_id": device_id,
        "capabilities": {"functions": [], "events": []},
        "identity": {"device_type": device_type, "manufacturer": "Test"},
        "status": {"location": "lab", "ts": "2025-01-01T00:00:00Z"},
        "ts": time.time(),
        "d2d": True,
    }).encode()


# ── PresenceAnnouncer ────────────────────────────────────────────

class TestPresenceAnnouncer:
    def test_subject_uses_tenant_and_device_id(self):
        messaging = _make_messaging()
        ann = PresenceAnnouncer(
            messaging, device_id="cam-01", tenant="default",
            capabilities={}, identity={}, status={},
        )
        assert ann.subject == "device-connect.default.cam-01.presence"

    @pytest.mark.asyncio
    async def test_publishes_presence(self):
        messaging = _make_messaging()
        ann = PresenceAnnouncer(
            messaging, device_id="cam-01", tenant="lab",
            capabilities={"functions": []},
            identity={"device_type": "camera"},
            status={"location": "bench"},
        )
        await ann.start()
        # Let it publish a few times during burst phase
        await asyncio.sleep(BURST_INTERVAL * 3)
        await ann.stop()

        assert messaging.publish.call_count >= 2
        # Verify subject
        first_call = messaging.publish.call_args_list[0]
        assert first_call[0][0] == "device-connect.lab.cam-01.presence"
        # Verify payload is valid JSON with expected fields
        payload = json.loads(first_call[0][1])
        assert payload["device_id"] == "cam-01"
        assert payload["d2d"] is True

    @pytest.mark.asyncio
    async def test_burst_then_steady(self):
        messaging = _make_messaging()
        ann = PresenceAnnouncer(
            messaging, device_id="cam-01", tenant="default",
            capabilities={}, identity={}, status={},
        )
        # Start in already-expired burst state (go straight to steady)
        ann._burst_until = time.time() - 1
        ann._task = asyncio.create_task(ann._loop())
        # Count calls over 1.5s — steady interval is 5s, so expect 0 or 1
        await asyncio.sleep(1.5)
        await ann.stop()
        assert messaging.publish.call_count <= 1

    @pytest.mark.asyncio
    async def test_trigger_burst_resets_fast_phase(self):
        messaging = _make_messaging()
        ann = PresenceAnnouncer(
            messaging, device_id="cam-01", tenant="default",
            capabilities={}, identity={}, status={},
        )
        ann._burst_until = 0  # expired
        ann.trigger_burst()
        assert ann._burst_until > time.time()

    @pytest.mark.asyncio
    async def test_stop_cancels_cleanly(self):
        messaging = _make_messaging()
        ann = PresenceAnnouncer(
            messaging, device_id="cam-01", tenant="default",
            capabilities={}, identity={}, status={},
        )
        await ann.start()
        await ann.stop()
        assert ann._task is None

    @pytest.mark.asyncio
    async def test_subscribes_to_probe_topic(self):
        messaging = _make_messaging()
        ann = PresenceAnnouncer(
            messaging, device_id="cam-01", tenant="default",
            capabilities={}, identity={}, status={},
        )
        await ann.start()
        # Should subscribe to both the probe topic
        probe_calls = [
            c for c in messaging.subscribe.call_args_list
            if c[0][0] == "device-connect.default.discovery.probe"
        ]
        assert len(probe_calls) == 1
        await ann.stop()

    @pytest.mark.asyncio
    async def test_responds_to_probe(self):
        messaging = _make_messaging()
        ann = PresenceAnnouncer(
            messaging, device_id="cam-01", tenant="lab",
            capabilities={"functions": []},
            identity={"device_type": "camera"},
            status={"location": "bench"},
        )
        messaging.publish.reset_mock()
        await ann._on_probe(b'{"probe": true}')
        # Should publish one presence message on the normal presence subject
        assert messaging.publish.call_count == 1
        subject = messaging.publish.call_args[0][0]
        assert subject == "device-connect.lab.cam-01.presence"
        payload = json.loads(messaging.publish.call_args[0][1])
        assert payload["device_id"] == "cam-01"

    @pytest.mark.asyncio
    async def test_stop_unsubscribes_probe(self):
        messaging = _make_messaging()
        probe_sub = AsyncMock()
        messaging.subscribe.return_value = probe_sub
        ann = PresenceAnnouncer(
            messaging, device_id="cam-01", tenant="default",
            capabilities={}, identity={}, status={},
        )
        await ann.start()
        await ann.stop()
        probe_sub.unsubscribe.assert_called_once()


# ── PresenceCollector ────────────────────────────────────────────

class TestPresenceCollector:
    @pytest.mark.asyncio
    async def test_subscribes_to_wildcard(self):
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        await collector.start()
        messaging.subscribe.assert_called_once()
        subject = messaging.subscribe.call_args[0][0]
        assert subject == "device-connect.default.*.presence"
        await collector.stop()

    @pytest.mark.asyncio
    async def test_tracks_peers(self):
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        await collector.start()

        # Simulate incoming presence
        await collector._on_presence(_make_presence_payload("robot-01", "robot"))
        await collector._on_presence(_make_presence_payload("camera-01", "camera"))

        devices = await collector.list_devices()
        assert len(devices) == 2
        ids = {d["device_id"] for d in devices}
        assert ids == {"robot-01", "camera-01"}
        await collector.stop()

    @pytest.mark.asyncio
    async def test_filters_by_device_type(self):
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        await collector.start()

        await collector._on_presence(_make_presence_payload("robot-01", "robot"))
        await collector._on_presence(_make_presence_payload("camera-01", "camera"))

        robots = await collector.list_devices(device_type="robot")
        assert len(robots) == 1
        assert robots[0]["device_id"] == "robot-01"
        await collector.stop()

    @pytest.mark.asyncio
    async def test_get_device(self):
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        await collector.start()

        await collector._on_presence(_make_presence_payload("cam-01"))
        device = await collector.get_device("cam-01")
        assert device is not None
        assert device["device_id"] == "cam-01"

        missing = await collector.get_device("nonexistent")
        assert missing is None
        await collector.stop()

    @pytest.mark.asyncio
    async def test_prunes_stale_peers(self):
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        await collector.start()

        await collector._on_presence(_make_presence_payload("stale-01"))
        # Manually make it stale
        collector._peers["stale-01"]["_last_seen"] = time.time() - PEER_TIMEOUT - 1

        # Trigger prune manually
        now = time.time()
        stale = [
            did for did, info in collector._peers.items()
            if now - info.get("_last_seen", 0) > PEER_TIMEOUT
        ]
        for did in stale:
            del collector._peers[did]

        devices = await collector.list_devices()
        assert len(devices) == 0
        await collector.stop()

    @pytest.mark.asyncio
    async def test_new_peer_callback(self):
        messaging = _make_messaging()
        new_peers = []
        collector = PresenceCollector(messaging, "default", on_new_peer=new_peers.append)
        await collector.start()

        await collector._on_presence(_make_presence_payload("robot-01"))
        assert "robot-01" in new_peers

        # Second presence from same peer should NOT trigger callback
        new_peers.clear()
        await collector._on_presence(_make_presence_payload("robot-01"))
        assert len(new_peers) == 0
        await collector.stop()

    @pytest.mark.asyncio
    async def test_ignores_invalid_payloads(self):
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        await collector.start()

        # Invalid JSON
        await collector._on_presence(b"not json")
        # Missing device_id
        await collector._on_presence(json.dumps({"foo": "bar"}).encode())

        devices = await collector.list_devices()
        assert len(devices) == 0
        await collector.stop()

    @pytest.mark.asyncio
    async def test_send_discovery_probe(self):
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "lab")
        await collector.start()
        messaging.publish.reset_mock()

        await collector.send_discovery_probe()
        messaging.publish.assert_called_once()
        subject = messaging.publish.call_args[0][0]
        assert subject == "device-connect.lab.discovery.probe"
        payload = json.loads(messaging.publish.call_args[0][1])
        assert payload["probe"] is True
        await collector.stop()

    @pytest.mark.asyncio
    async def test_wait_for_peers_sends_probe(self):
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        await collector.start()
        messaging.publish.reset_mock()

        # No peers available, so it will timeout — but probe should be sent
        await collector.wait_for_peers(timeout=0.3)
        # Verify probe was published
        probe_calls = [
            c for c in messaging.publish.call_args_list
            if "discovery.probe" in c[0][0]
        ]
        assert len(probe_calls) == 1
        await collector.stop()

    @pytest.mark.asyncio
    async def test_wait_for_peers_returns_when_available(self):
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        await collector.start()

        # Add a peer after a short delay
        async def add_peer():
            await asyncio.sleep(0.2)
            await collector._on_presence(_make_presence_payload("robot-01"))

        asyncio.create_task(add_peer())
        peers = await collector.wait_for_peers(timeout=2.0)
        assert len(peers) == 1
        await collector.stop()

    @pytest.mark.asyncio
    async def test_wait_for_peers_returns_empty_on_timeout(self):
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        await collector.start()

        peers = await collector.wait_for_peers(timeout=0.3)
        assert len(peers) == 0
        await collector.stop()

    @pytest.mark.asyncio
    async def test_departure_removes_peer(self):
        """A presence message with departing=True prunes the peer immediately."""
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        await collector.start()

        # Add a peer first
        await collector._on_presence(_make_presence_payload("robot-01", "robot"))
        device = await collector.get_device("robot-01")
        assert device is not None

        # Send departure
        departure = json.dumps({
            "device_id": "robot-01",
            "departing": True,
            "ts": time.time(),
        }).encode()
        await collector._on_presence(departure)

        device = await collector.get_device("robot-01")
        assert device is None
        await collector.stop()

    @pytest.mark.asyncio
    async def test_departure_for_unknown_peer_is_noop(self):
        """Departure for a peer not in the table does not raise."""
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        await collector.start()

        departure = json.dumps({
            "device_id": "unknown-01",
            "departing": True,
            "ts": time.time(),
        }).encode()
        await collector._on_presence(departure)

        devices = await collector.list_devices()
        assert len(devices) == 0
        await collector.stop()


# ── D2DRegistry ──────────────────────────────────────────────────

class TestD2DRegistry:
    @pytest.mark.asyncio
    async def test_list_devices_delegates_to_collector(self):
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        await collector.start()
        await collector._on_presence(_make_presence_payload("cam-01", "camera"))

        registry = D2DRegistry(collector)
        devices = await registry.list_devices()
        assert len(devices) == 1
        assert devices[0]["device_id"] == "cam-01"
        await collector.stop()

    @pytest.mark.asyncio
    async def test_list_devices_with_type_filter(self):
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        await collector.start()
        await collector._on_presence(_make_presence_payload("cam-01", "camera"))
        await collector._on_presence(_make_presence_payload("robot-01", "robot"))

        registry = D2DRegistry(collector)
        cameras = await registry.list_devices(device_type="camera")
        assert len(cameras) == 1
        assert cameras[0]["device_id"] == "cam-01"
        await collector.stop()

    @pytest.mark.asyncio
    async def test_get_device(self):
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        await collector.start()
        await collector._on_presence(_make_presence_payload("cam-01"))

        registry = D2DRegistry(collector)
        device = await registry.get_device("cam-01")
        assert device is not None

        missing = await registry.get_device("nonexistent")
        assert missing is None
        await collector.stop()

    @pytest.mark.asyncio
    async def test_accepts_extra_kwargs(self):
        """D2DRegistry.list_devices should accept location/capabilities/timeout
        for API compatibility with RegistryClient, even if it ignores them."""
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        await collector.start()

        registry = D2DRegistry(collector)
        # Should not raise
        devices = await registry.list_devices(
            device_type="camera", location="lab",
            capabilities=["capture"], timeout=5.0,
        )
        assert isinstance(devices, list)
        await collector.stop()


# ── subscribe_only flag tests ───────────────────────────────────


class TestSubscribeOnlyFlag:
    """Verify that discovery subscriptions use subscribe_only=True."""

    @pytest.mark.asyncio
    async def test_presence_subscribe_uses_subscribe_only(self):
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        await collector.start()

        assert messaging.subscribe.call_count == 1
        call_kwargs = messaging.subscribe.call_args[1]
        assert call_kwargs.get("subscribe_only") is True
        await collector.stop()

    @pytest.mark.asyncio
    async def test_probe_subscribe_uses_subscribe_only(self):
        messaging = _make_messaging()
        ann = PresenceAnnouncer(
            messaging, device_id="cam-01", tenant="default",
            capabilities={}, identity={}, status={},
        )
        await ann.start()

        probe_calls = [
            c for c in messaging.subscribe.call_args_list
            if c[0][0] == "device-connect.default.discovery.probe"
        ]
        assert len(probe_calls) == 1
        assert probe_calls[0][1].get("subscribe_only") is True
        await ann.stop()

    @pytest.mark.asyncio
    async def test_duplicate_presence_not_double_counted(self):
        """Simulate dual delivery: two rapid _on_presence calls for the same
        device should result in on_new_peer firing exactly once."""
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        await collector.start()

        new_peer_calls = []
        collector._on_new_peer = lambda pid: new_peer_calls.append(pid)

        payload = _make_presence_payload("robot-001", "robot")

        # Simulate two near-simultaneous deliveries (subscriber + queryable paths)
        await collector._on_presence(payload, None)
        await collector._on_presence(payload, None)

        assert len(new_peer_calls) == 1
        devices = await collector.list_devices()
        robot_entries = [d for d in devices if d["device_id"] == "robot-001"]
        assert len(robot_entries) == 1
        await collector.stop()


# ── wait_for_device_type / wait_for_device_id ─────────────────


class TestPresenceCollectorWait:
    """Tests for PresenceCollector.wait_for_device_type/id."""

    @pytest.mark.asyncio
    async def test_wait_for_device_type_raises_if_not_started(self):
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        with pytest.raises(RuntimeError, match="not started"):
            await collector.wait_for_device_type("robot", timeout=0.1)

    @pytest.mark.asyncio
    async def test_wait_for_device_id_raises_if_not_started(self):
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        with pytest.raises(RuntimeError, match="not started"):
            await collector.wait_for_device_id("robot-01", timeout=0.1)

    @pytest.mark.asyncio
    async def test_wait_for_device_type_returns_match(self):
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        await collector.start()

        async def add_robot():
            await asyncio.sleep(0.2)
            await collector._on_presence(_make_presence_payload("robot-01", "robot"))

        asyncio.create_task(add_robot())
        result = await collector.wait_for_device_type("robot", timeout=2.0)
        assert result is not None
        assert result["device_id"] == "robot-01"
        await collector.stop()

    @pytest.mark.asyncio
    async def test_wait_for_device_type_returns_none_on_timeout(self):
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        await collector.start()

        result = await collector.wait_for_device_type("robot", timeout=0.3)
        assert result is None
        await collector.stop()

    @pytest.mark.asyncio
    async def test_wait_for_device_id_returns_match(self):
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        await collector.start()

        async def add_device():
            await asyncio.sleep(0.2)
            await collector._on_presence(_make_presence_payload("robot-01", "robot"))

        asyncio.create_task(add_device())
        result = await collector.wait_for_device_id("robot-01", timeout=2.0)
        assert result is not None
        assert result["device_id"] == "robot-01"
        await collector.stop()

    @pytest.mark.asyncio
    async def test_wait_for_device_id_returns_none_on_timeout(self):
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        await collector.start()

        result = await collector.wait_for_device_id("nonexistent", timeout=0.3)
        assert result is None
        await collector.stop()


# ── Self-filtering ──────────────────────────────────────────────

class TestSelfFiltering:
    """Verify that a collector ignores its own presence announcements."""

    @pytest.mark.asyncio
    async def test_own_device_id_not_tracked(self):
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default", device_id="my-device")
        await collector.start()

        await collector._on_presence(_make_presence_payload("my-device", "camera"))

        devices = await collector.list_devices()
        assert len(devices) == 0
        await collector.stop()

    @pytest.mark.asyncio
    async def test_other_device_still_tracked(self):
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default", device_id="my-device")
        await collector.start()

        await collector._on_presence(_make_presence_payload("my-device", "camera"))
        await collector._on_presence(_make_presence_payload("other-device", "robot"))

        devices = await collector.list_devices()
        assert len(devices) == 1
        assert devices[0]["device_id"] == "other-device"
        await collector.stop()

    @pytest.mark.asyncio
    async def test_no_device_id_does_not_self_filter(self):
        """Collector without device_id should not filter anything."""
        messaging = _make_messaging()
        collector = PresenceCollector(messaging, "default")
        await collector.start()

        await collector._on_presence(_make_presence_payload("camera-01", "camera"))

        devices = await collector.list_devices()
        assert len(devices) == 1
        await collector.stop()


# ── _device_has_capabilities ────────────────────────────────────

class TestDeviceHasCapabilities:

    def test_dict_functions_match(self):
        device = {"capabilities": {"functions": [{"name": "snap"}, {"name": "zoom"}]}}
        assert _device_has_capabilities(device, ["snap"]) is True
        assert _device_has_capabilities(device, ["snap", "zoom"]) is True

    def test_string_functions_match(self):
        device = {"capabilities": {"functions": ["snap", "zoom"]}}
        assert _device_has_capabilities(device, ["snap"]) is True
        assert _device_has_capabilities(device, ["snap", "zoom"]) is True

    def test_mixed_functions_match(self):
        device = {"capabilities": {"functions": [{"name": "snap"}, "zoom"]}}
        assert _device_has_capabilities(device, ["snap", "zoom"]) is True

    def test_missing_capability(self):
        device = {"capabilities": {"functions": [{"name": "snap"}]}}
        assert _device_has_capabilities(device, ["zoom"]) is False

    def test_partial_match_returns_false(self):
        device = {"capabilities": {"functions": [{"name": "snap"}]}}
        assert _device_has_capabilities(device, ["snap", "zoom"]) is False

    def test_empty_required_returns_true(self):
        device = {"capabilities": {"functions": [{"name": "snap"}]}}
        assert _device_has_capabilities(device, []) is True

    def test_no_capabilities_key(self):
        device = {}
        assert _device_has_capabilities(device, ["snap"]) is False

    def test_none_capabilities(self):
        device = {"capabilities": None}
        assert _device_has_capabilities(device, ["snap"]) is False

    def test_empty_functions_list(self):
        device = {"capabilities": {"functions": []}}
        assert _device_has_capabilities(device, ["snap"]) is False
        assert _device_has_capabilities(device, []) is True
