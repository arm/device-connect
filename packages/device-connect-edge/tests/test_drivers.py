# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for device_connect_edge.drivers module.

Tests @rpc, @emit, @on decorators, schema generation, and DeviceDriver base class.
"""


import pytest
from unittest.mock import AsyncMock, MagicMock

from device_connect_edge.drivers import DeviceDriver, rpc, emit, build_function_schema, build_event_schema
from device_connect_edge.drivers.base import on
from device_connect_edge.types import DeviceIdentity, DeviceStatus


# ── @rpc decorator ─────────────────────────────────────────────────

class TestRpc:
    def test_marks_function(self):
        @rpc()
        async def my_func(self, x: int) -> dict:
            """Do something."""
            return {"x": x}

        assert my_func._is_device_function is True
        assert my_func._function_name == "my_func"

    def test_custom_name(self):
        @rpc(name="customName")
        async def my_func(self) -> dict:
            """A function."""
            return {}

        assert my_func._function_name == "customName"

    def test_description_from_docstring(self):
        @rpc()
        async def capture(self, resolution: str = "1080p") -> dict:
            """Capture an image from the camera.

            Args:
                resolution: Image resolution
            """
            return {}

        assert capture._description == "Capture an image from the camera."

    def test_custom_description(self):
        @rpc(description="Custom desc")
        async def func(self) -> dict:
            """Original."""
            return {}

        assert func._description == "Custom desc"


# ── @emit decorator ────────────────────────────────────────────────

class TestEmit:
    def test_marks_event(self):
        @emit("object_detected")
        async def object_detected(self, label: str):
            """Object detected."""
            pass

        assert object_detected._is_device_event is True
        assert object_detected._event_name == "object_detected"

    def test_event_description(self):
        @emit("motion_detected")
        async def motion_detected(self, zone: str):
            """Motion detected in zone.

            Args:
                zone: Zone identifier
            """
            pass

        assert motion_detected._event_description == "Motion detected in zone."

    def test_inferred_event_name(self):
        @emit()
        async def alert_triggered(self, level: str):
            """Alert triggered."""
            pass

        assert alert_triggered._event_name == "alert_triggered"


# ── Schema generation ──────────────────────────────────────────────

class TestBuildFunctionSchema:
    def test_simple_schema(self):
        @rpc()
        async def func(self, name: str, count: int = 10) -> dict:
            """A function."""
            return {}

        schema = build_function_schema(func)
        # Schema is a JSON Schema object with properties at top level
        assert "properties" in schema
        assert "name" in schema["properties"]
        assert "count" in schema["properties"]

    def test_no_params(self):
        @rpc()
        async def ping(self) -> dict:
            """Ping."""
            return {}

        schema = build_function_schema(ping)
        assert schema["type"] == "object"


class TestBuildEventSchema:
    def test_event_schema(self):
        @emit("reading")
        async def reading(self, temperature: float, humidity: float):
            """Sensor reading."""
            pass

        schema = build_event_schema(reading)
        assert "properties" in schema
        assert "temperature" in schema["properties"]
        assert "humidity" in schema["properties"]


# ── DeviceDriver base class ───────────────────────────────────────

class SampleDriver(DeviceDriver):
    device_type = "sample"

    @property
    def identity(self) -> DeviceIdentity:
        return DeviceIdentity(device_type="sample", manufacturer="Test", model="S1", firmware_version="0.1")

    @property
    def status(self) -> DeviceStatus:
        return DeviceStatus(location="lab")

    @rpc()
    async def do_something(self, value: int) -> dict:
        """Do something."""
        return {"result": value * 2}

    @emit()
    async def something_happened(self, detail: str):
        """Something happened."""
        pass

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass


class TestDeviceDriverBase:
    def test_identity(self):
        driver = SampleDriver()
        assert driver.identity.device_type == "sample"

    def test_status(self):
        driver = SampleDriver()
        assert driver.status.location == "lab"

    def test_device_type(self):
        driver = SampleDriver()
        assert driver.device_type == "sample"

    @pytest.mark.asyncio
    async def test_rpc_callable(self):
        driver = SampleDriver()
        result = await driver.do_something(value=5)
        assert result == {"result": 10}


# -- Discovery labels (Phase 1) ------------------------------------

class TestRpcLabels:
    def test_default_none(self):
        @rpc()
        async def f(self) -> dict:
            """f."""
            return {}

        assert f._labels is None

    def test_explicit_labels(self):
        @rpc(labels={"direction": "write", "modality": ["rgb", "4k"]})
        async def capture(self, resolution: str = "1080p") -> dict:
            """Capture."""
            return {}

        assert capture._labels == {"direction": "write", "modality": ["rgb", "4k"]}


class TestEmitLabels:
    def test_default_none(self):
        @emit()
        async def heartbeat(self):
            """heartbeat."""
            pass

        assert heartbeat._labels is None

    def test_explicit_labels(self):
        @emit(labels={"modality": "motion", "safety": "informational"})
        async def motion_detected(self, zone: str):
            """Motion."""
            pass

        assert motion_detected._labels == {"modality": "motion", "safety": "informational"}


class LabeledDriver(DeviceDriver):
    """Driver with class-level labels and per-method labels."""
    device_type = "camera"
    labels = {
        "category": ["camera", "inference"],
        "location": "warehouse1/loading-dock",
    }

    @rpc(labels={"direction": "write", "modality": ["rgb", "4k"]})
    async def capture_frame(self, resolution: str = "1080p") -> dict:
        """Capture a frame."""
        return {}

    @rpc()
    async def ping(self) -> dict:
        """Ping."""
        return {}

    @emit(labels={"modality": "motion", "safety": "informational"})
    async def motion_detected(self, zone: str, confidence: float):
        """Motion in zone."""
        pass


class TestDriverLabels:
    def test_class_level_labels_on_capabilities(self):
        caps = LabeledDriver().capabilities
        assert caps.labels == {
            "category": ["camera", "inference"],
            "location": "warehouse1/loading-dock",
        }

    def test_function_labels_propagated(self):
        caps = LabeledDriver().capabilities
        fns = {f.name: f for f in caps.functions}
        assert fns["capture_frame"].labels == {"direction": "write", "modality": ["rgb", "4k"]}
        assert fns["ping"].labels is None

    def test_event_labels_propagated(self):
        caps = LabeledDriver().capabilities
        evs = {e.name: e for e in caps.events}
        assert evs["motion_detected"].labels == {"modality": "motion", "safety": "informational"}

    def test_no_class_labels_defaults_to_none(self):
        # SampleDriver above does NOT define `labels` -- inherits None from DeviceDriver
        assert SampleDriver().capabilities.labels is None

    def test_capabilities_detected(self):
        """Driver should have functions and events detectable via introspection."""
        driver = SampleDriver()
        # Check that decorated methods are discoverable
        funcs = [
            m for m in dir(driver)
            if getattr(getattr(driver, m, None), "_is_device_function", False)
        ]
        events = [
            m for m in dir(driver)
            if getattr(getattr(driver, m, None), "_is_device_event", False)
        ]
        # At least our decorated methods should be found
        func_names = [getattr(getattr(driver, m), "_function_name") for m in funcs]
        event_names = [getattr(getattr(driver, m), "_event_name") for m in events]
        assert "do_something" in func_names
        assert "something_happened" in event_names


# ── @on decorator ─────────────────────────────────────────────────

class TestOn:
    def test_marks_event_subscription(self):
        @on(device_type="camera", event_name="motion_detected")
        async def handler(self, device_id, event_name, payload):
            pass

        assert handler._is_event_subscription is True
        assert handler._sub_device_type == "camera"
        assert handler._sub_event_name == "motion_detected"
        assert handler._sub_device_id is None

    def test_with_device_id(self):
        @on(device_id="cam-001", event_name="alert")
        async def handler(self, device_id, event_name, payload):
            pass

        assert handler._sub_device_id == "cam-001"
        assert handler._sub_device_type is None

    def test_all_params(self):
        @on(device_id="robot-1", device_type="robot", event_name="done")
        async def handler(self, device_id, event_name, payload):
            pass

        assert handler._sub_device_id == "robot-1"
        assert handler._sub_device_type == "robot"
        assert handler._sub_event_name == "done"

    def test_defaults_to_none(self):
        @on()
        async def handler(self, device_id, event_name, payload):
            pass

        assert handler._is_event_subscription is True
        assert handler._sub_device_id is None
        assert handler._sub_device_type is None
        assert handler._sub_event_name is None


# ── _collect_event_subscriptions ──────────────────────────────────

class TestCollectEventSubscriptions:
    def test_collects_on_methods(self):
        class MyDriver(DeviceDriver):
            device_type = "test"

            @on(device_type="camera", event_name="motion")
            async def on_motion(self, device_id, event_name, payload):
                pass

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        driver = MyDriver()
        subs = driver._collect_event_subscriptions()
        assert len(subs) == 1
        assert subs[0]["device_type"] == "camera"
        assert subs[0]["event_name"] == "motion"

    def test_ignores_rpc_methods(self):
        class MyDriver(DeviceDriver):
            device_type = "test"

            @rpc()
            async def do_thing(self) -> dict:
                """A function."""
                return {}

            @on(device_type="sensor")
            async def on_reading(self, device_id, event_name, payload):
                pass

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        driver = MyDriver()
        subs = driver._collect_event_subscriptions()
        assert len(subs) == 1
        assert subs[0]["device_type"] == "sensor"

    def test_multiple_subscriptions(self):
        class MyDriver(DeviceDriver):
            device_type = "test"

            @on(device_type="camera", event_name="motion")
            async def on_motion(self, device_id, event_name, payload):
                pass

            @on(device_id="robot-001", event_name="done")
            async def on_done(self, device_id, event_name, payload):
                pass

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        driver = MyDriver()
        subs = driver._collect_event_subscriptions()
        assert len(subs) == 2

    def test_underscore_prefixed_handler_is_still_collected(self):
        """Single-underscore @on handlers must not silently become no-ops."""
        class MyDriver(DeviceDriver):
            device_type = "test"

            @on(device_type="phone", event_name="state_changed")
            async def _on_phone_state(self, device_id, event_name, payload):
                pass

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        driver = MyDriver()
        subs = driver._collect_event_subscriptions()
        assert len(subs) == 1
        assert subs[0]["device_type"] == "phone"
        assert subs[0]["event_name"] == "state_changed"


# ── setup_subscriptions error isolation ───────────────────────────

class TestSetupSubscriptionsErrorIsolation:
    @pytest.mark.asyncio
    async def test_one_failure_does_not_block_others(self):
        """If one subscription fails, the rest should still be set up."""

        class MyDriver(DeviceDriver):
            device_type = "test"

            @on(device_type="camera", event_name="motion")
            async def on_motion(self, device_id, event_name, payload):
                pass

            @on(device_type="sensor", event_name="reading")
            async def on_reading(self, device_id, event_name, payload):
                pass

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        driver = MyDriver()

        # Use a simple object (not MagicMock) to avoid MagicMock's
        # auto-attribute creation leaking into DeviceDriver introspection.
        mock_messaging = AsyncMock()
        call_count = 0

        async def fail_then_succeed(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("first subscription fails")
            return MagicMock()  # subscription handle

        mock_messaging.subscribe_with_subject = AsyncMock(side_effect=fail_then_succeed)

        class FakeRouter:
            def __init__(self):
                self._messaging = mock_messaging
                self._tenant = "default"

        driver._router = FakeRouter()

        await driver.setup_subscriptions()

        # Both were attempted
        assert mock_messaging.subscribe_with_subject.await_count == 2
        # Only the second (successful) subscription was tracked
        assert len(driver._subscriptions) == 1


# ── lifecycle subscriptions (device.online / device.offline) ──────

class TestLifecycleSubscriptions:
    """The registry publishes presence changes on a shared subject per
    tenant: `device-connect.<tenant>.device.{online,offline}`. Drivers
    use @on(event_name=...) to subscribe; both canonical names and
    `peer_present`/`peer_lost` aliases must work."""

    async def _setup_capture(self, driver):
        """Wire driver to a fake router; return the captured subject + handler."""
        captured = {}

        async def fake_subscribe(subject, callback, **kwargs):
            captured["subject"] = subject
            captured["callback"] = callback
            return MagicMock()

        mock_messaging = AsyncMock()
        mock_messaging.subscribe_with_subject = AsyncMock(side_effect=fake_subscribe)

        class FakeRouter:
            def __init__(self):
                self._messaging = mock_messaging
                self._tenant = "beta"

        driver._router = FakeRouter()
        await driver.setup_subscriptions()
        return captured

    @pytest.mark.asyncio
    async def test_device_offline_subscribes_to_lifecycle_subject(self):
        seen = []

        class MyDriver(DeviceDriver):
            device_type = "test"

            @on(event_name="device.offline")
            async def on_lost(self, device_id, event_name, payload):
                seen.append((device_id, event_name, payload))

            async def connect(self): pass
            async def disconnect(self): pass

        driver = MyDriver()
        captured = await self._setup_capture(driver)

        # Subscribed to the registry's lifecycle subject, not to a
        # per-device .event.<name> subject.
        assert captured["subject"] == "device-connect.beta.device.offline"

        # Simulate a registry-published offline event.
        import json
        msg = json.dumps({
            "jsonrpc": "2.0",
            "method": "device/offline",
            "params": {"device_id": "interlock-01", "ts": "2026-05-06T00:00:00Z"},
        }).encode()
        await captured["callback"](msg, captured["subject"], None)

        assert seen == [(
            "interlock-01",
            "device.offline",
            {"device_id": "interlock-01", "ts": "2026-05-06T00:00:00Z"},
        )]

    @pytest.mark.asyncio
    async def test_peer_lost_alias_resolves_to_offline_subject(self):
        class MyDriver(DeviceDriver):
            device_type = "test"

            @on(event_name="peer_lost")
            async def on_peer_lost(self, device_id, event_name, payload):
                pass

            async def connect(self): pass
            async def disconnect(self): pass

        driver = MyDriver()
        captured = await self._setup_capture(driver)
        assert captured["subject"] == "device-connect.beta.device.offline"

    @pytest.mark.asyncio
    async def test_peer_present_alias_resolves_to_online_subject(self):
        class MyDriver(DeviceDriver):
            device_type = "test"

            @on(event_name="peer_present")
            async def on_peer_present(self, device_id, event_name, payload):
                pass

            async def connect(self): pass
            async def disconnect(self): pass

        driver = MyDriver()
        captured = await self._setup_capture(driver)
        assert captured["subject"] == "device-connect.beta.device.online"

    @pytest.mark.asyncio
    async def test_device_id_filter_drops_other_devices(self):
        seen = []

        class MyDriver(DeviceDriver):
            device_type = "test"

            @on(device_id="interlock-01", event_name="peer_lost")
            async def on_lost(self, device_id, event_name, payload):
                seen.append(device_id)

            async def connect(self): pass
            async def disconnect(self): pass

        driver = MyDriver()
        captured = await self._setup_capture(driver)

        import json
        # An unrelated device dropping out -- handler should NOT fire.
        unrelated = json.dumps({
            "method": "device/offline",
            "params": {"device_id": "camera-99"},
        }).encode()
        await captured["callback"](unrelated, captured["subject"], None)
        assert seen == []

        # The device the handler cares about -- handler fires.
        target = json.dumps({
            "method": "device/offline",
            "params": {"device_id": "interlock-01"},
        }).encode()
        await captured["callback"](target, captured["subject"], None)
        assert seen == ["interlock-01"]

    @pytest.mark.asyncio
    async def test_per_device_event_path_unchanged(self):
        """Non-lifecycle events still build the per-device subject."""

        class MyDriver(DeviceDriver):
            device_type = "test"

            @on(device_id="cam-001", event_name="motion_detected")
            async def on_motion(self, device_id, event_name, payload):
                pass

            async def connect(self): pass
            async def disconnect(self): pass

        driver = MyDriver()
        captured = await self._setup_capture(driver)
        assert captured["subject"] == "device-connect.beta.cam-001.event.motion_detected"

    @pytest.mark.asyncio
    async def test_glob_device_id_lifecycle_filters_in_handler(self):
        """device_id='interlock-*' on lifecycle: subscribe to shared
        subject, filter by fnmatch in the handler."""
        seen = []

        class MyDriver(DeviceDriver):
            device_type = "test"

            @on(device_id="interlock-*", event_name="peer_lost")
            async def on_lost(self, device_id, event_name, payload):
                seen.append(device_id)

            async def connect(self): pass
            async def disconnect(self): pass

        driver = MyDriver()
        captured = await self._setup_capture(driver)
        assert captured["subject"] == "device-connect.beta.device.offline"

        import json
        for did, should_match in [
            ("interlock-01", True),
            ("interlock-99", True),
            ("laser-01",     False),
            ("interlock",    False),  # no suffix, glob requires at least one char after the dash
        ]:
            msg = json.dumps({
                "method": "device/offline",
                "params": {"device_id": did},
            }).encode()
            await captured["callback"](msg, captured["subject"], None)

        assert seen == ["interlock-01", "interlock-99"]

    @pytest.mark.asyncio
    async def test_glob_device_id_per_device_uses_broker_wildcard(self):
        """device_id='cam-*' on a non-lifecycle event subscribes to
        the broker-wildcard subject and filters in the handler."""
        seen = []

        class MyDriver(DeviceDriver):
            device_type = "test"

            @on(device_id="cam-*", event_name="motion")
            async def on_motion(self, device_id, event_name, payload):
                seen.append(device_id)

            async def connect(self): pass
            async def disconnect(self): pass

        driver = MyDriver()
        captured = await self._setup_capture(driver)
        # Glob -> broker wildcard, not the literal pattern.
        assert captured["subject"] == "device-connect.beta.*.event.motion"

        import json
        msg_body = json.dumps({"method": "motion", "params": {}}).encode()
        # Simulate broker delivering events from three different devices.
        for did in ("cam-001", "cam-rear", "robot-7"):
            await captured["callback"](
                msg_body, f"device-connect.beta.{did}.event.motion", None,
            )
        assert seen == ["cam-001", "cam-rear"]


class TestLifecycleSubscriptionsD2D:
    """D2D mode has no registry to publish ``device.{online,offline}``.

    Lifecycle ``@on`` handlers must be delivered through the
    PresenceCollector's add/remove callbacks instead. Per-device event
    subscriptions still go through the broker, regardless of mode.
    """

    async def _setup_d2d(self, driver):
        """Wire driver to a fake router + a real-ish PresenceCollector stand-in."""
        from device_connect_edge.discovery import PresenceCollector

        class FakeMessaging:
            async def subscribe(self, *a, **kw): return MagicMock()
            async def subscribe_with_subject(self, *a, **kw): return MagicMock()

        class FakeDevice:
            def __init__(self, collector):
                self._d2d_collector = collector

        class FakeRouter:
            def __init__(self, messaging):
                self._messaging = messaging
                self._tenant = "beta"

        messaging = FakeMessaging()
        collector = PresenceCollector(messaging, tenant="beta", device_id="self")
        driver._router = FakeRouter(messaging)
        driver._device = FakeDevice(collector)
        await driver.setup_subscriptions()
        return collector

    @pytest.mark.asyncio
    async def test_peer_lost_routes_through_collector(self):
        """In D2D mode, @on(event_name='peer_lost') registers an
        on_peer_removed listener instead of subscribing to a subject."""
        seen = []

        class MyDriver(DeviceDriver):
            device_type = "test"

            @on(event_name="peer_lost")
            async def on_lost(self, device_id, event_name, payload):
                seen.append((device_id, event_name, payload))

            async def connect(self): pass
            async def disconnect(self): pass

        driver = MyDriver()
        collector = await self._setup_d2d(driver)

        # Listener is registered on the collector, NOT a broker
        # subscription.
        assert len(collector._peer_removed_listeners) == 1

        # Trigger the listener directly.
        await collector._emit_peer_removed("interlock-01")
        assert seen == [("interlock-01", "device.offline", {})]

    @pytest.mark.asyncio
    async def test_peer_present_routes_through_collector(self):
        seen = []

        class MyDriver(DeviceDriver):
            device_type = "test"

            @on(event_name="peer_present")
            async def on_present(self, device_id, event_name, payload):
                seen.append((device_id, event_name, payload))

            async def connect(self): pass
            async def disconnect(self): pass

        driver = MyDriver()
        collector = await self._setup_d2d(driver)

        assert len(collector._new_peer_listeners) == 1

        await collector._emit_new_peer("camera-7")
        assert seen == [("camera-7", "device.online", {})]

    @pytest.mark.asyncio
    async def test_d2d_lifecycle_device_id_filter_exact(self):
        """device_id= filter applies in the D2D delivery path too."""
        seen = []

        class MyDriver(DeviceDriver):
            device_type = "test"

            @on(device_id="interlock-01", event_name="peer_lost")
            async def on_lost(self, device_id, event_name, payload):
                seen.append(device_id)

            async def connect(self): pass
            async def disconnect(self): pass

        driver = MyDriver()
        collector = await self._setup_d2d(driver)

        # Unrelated device -- handler should not fire.
        await collector._emit_peer_removed("camera-99")
        assert seen == []

        # Target device -- handler fires.
        await collector._emit_peer_removed("interlock-01")
        assert seen == ["interlock-01"]

    @pytest.mark.asyncio
    async def test_d2d_lifecycle_device_id_filter_glob(self):
        """Glob device_id= filter applies in the D2D delivery path."""
        seen = []

        class MyDriver(DeviceDriver):
            device_type = "test"

            @on(device_id="interlock-*", event_name="peer_lost")
            async def on_lost(self, device_id, event_name, payload):
                seen.append(device_id)

            async def connect(self): pass
            async def disconnect(self): pass

        driver = MyDriver()
        collector = await self._setup_d2d(driver)

        for did in ("interlock-01", "interlock-99", "laser-01"):
            await collector._emit_peer_removed(did)
        assert seen == ["interlock-01", "interlock-99"]

    @pytest.mark.asyncio
    async def test_d2d_per_device_event_still_uses_broker(self):
        """Per-device events go through the broker even in D2D mode."""
        from device_connect_edge.discovery import PresenceCollector

        class MyDriver(DeviceDriver):
            device_type = "test"

            @on(device_id="cam-001", event_name="motion_detected")
            async def on_motion(self, device_id, event_name, payload):
                pass

            async def connect(self): pass
            async def disconnect(self): pass

        captured = {}

        async def fake_subscribe(subject, callback, **kwargs):
            captured["subject"] = subject
            return MagicMock()

        messaging = AsyncMock()
        messaging.subscribe_with_subject = AsyncMock(side_effect=fake_subscribe)

        class FakeDevice:
            def __init__(self, collector):
                self._d2d_collector = collector

        class FakeRouter:
            def __init__(self):
                self._messaging = messaging
                self._tenant = "beta"

        collector = PresenceCollector(messaging, tenant="beta", device_id="self")
        driver = MyDriver()
        driver._router = FakeRouter()
        driver._device = FakeDevice(collector)
        await driver.setup_subscriptions()

        # Per-device event still goes through the broker subscription;
        # nothing is wired into the collector lifecycle listeners.
        assert captured["subject"] == "device-connect.beta.cam-001.event.motion_detected"
        assert collector._new_peer_listeners == []
        assert collector._peer_removed_listeners == []


class TestPresenceCollectorRemovedCallback:
    """Symmetric callback for peer removals (graceful + timeout)."""

    @pytest.mark.asyncio
    async def test_graceful_departure_fires_on_peer_removed(self):
        import json
        from device_connect_edge.discovery import PresenceCollector

        seen = []

        async def on_removed(device_id):
            seen.append(device_id)

        messaging = AsyncMock()
        collector = PresenceCollector(
            messaging, tenant="beta", device_id="self",
            on_peer_removed=on_removed,
        )

        # Seed: peer is currently known.
        collector._peers["interlock-01"] = {"_last_seen": 0}

        # Inject a graceful departure message.
        msg = json.dumps({
            "device_id": "interlock-01",
            "departing": True,
        }).encode()
        await collector._on_presence(msg)

        assert seen == ["interlock-01"]
        assert "interlock-01" not in collector._peers

    @pytest.mark.asyncio
    async def test_prune_timeout_fires_on_peer_removed(self):
        """Stale peers pruned by _prune_loop fire on_peer_removed."""
        import time
        from device_connect_edge.discovery import PresenceCollector

        seen = []

        async def on_removed(device_id):
            seen.append(device_id)

        messaging = AsyncMock()
        collector = PresenceCollector(
            messaging, tenant="beta", device_id="self",
            on_peer_removed=on_removed,
        )

        # Seed: a peer whose _last_seen is way in the past.
        collector._peers["camera-99"] = {"_last_seen": time.time() - 10_000}

        # Walk the prune body directly (avoid waiting on the sleep loop).
        async with collector._lock:
            stale = [
                did for did, info in collector._peers.items()
                if time.time() - info.get("_last_seen", 0) > 1
            ]
            for did in stale:
                del collector._peers[did]
        for did in stale:
            await collector._emit_peer_removed(did)

        assert seen == ["camera-99"]

    @pytest.mark.asyncio
    async def test_add_on_peer_removed_supports_multiple_listeners(self):
        """Multiple @on handlers can register without clobbering each other."""
        from device_connect_edge.discovery import PresenceCollector

        seen_a, seen_b = [], []

        async def cb_a(d): seen_a.append(d)
        async def cb_b(d): seen_b.append(d)

        collector = PresenceCollector(AsyncMock(), tenant="beta", device_id="self")
        collector.add_on_peer_removed(cb_a)
        collector.add_on_peer_removed(cb_b)

        await collector._emit_peer_removed("rig-7")
        assert seen_a == ["rig-7"]
        assert seen_b == ["rig-7"]

    @pytest.mark.asyncio
    async def test_constructor_callback_coexists_with_listeners(self):
        """Single-listener constructor pattern keeps working."""
        from device_connect_edge.discovery import PresenceCollector

        seen_ctor, seen_added = [], []

        async def ctor_cb(d): seen_ctor.append(d)
        async def added_cb(d): seen_added.append(d)

        collector = PresenceCollector(
            AsyncMock(), tenant="beta", device_id="self",
            on_new_peer=ctor_cb,
        )
        collector.add_on_new_peer(added_cb)

        await collector._emit_new_peer("peer-1")
        assert seen_ctor == ["peer-1"]
        assert seen_added == ["peer-1"]
