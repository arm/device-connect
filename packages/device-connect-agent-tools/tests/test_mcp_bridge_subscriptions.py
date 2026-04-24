"""Unit tests for the MCP bridge's event subscription manager.

These tests exercise EventSubscriptionManager in isolation — a fake messaging
client captures the subscribe callback so the test can fire synthetic events,
and a fake ServerSession records send_resource_updated calls so we can assert
the right URIs were notified. No actual MCP transport, no Device Connect
fabric, no FastMCP server.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Callable

import pytest

from device_connect_agent_tools.mcp import event_subscriptions as es


# ── Fakes ──────────────────────────────────────────────────────────────


class _FakeFabricSub:
    """Stand-in for the subscription handle returned by MessagingClient."""

    def __init__(self) -> None:
        self.unsubscribed = False

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


@dataclass
class _FakeMessagingClient:
    """Captures the (subject, callback) pairs the manager registers."""

    subs: dict[str, tuple[Callable, _FakeFabricSub]] = field(default_factory=dict)

    async def subscribe_with_subject(self, subject: str, callback: Callable) -> _FakeFabricSub:
        sub = _FakeFabricSub()
        self.subs[subject] = (callback, sub)
        return sub

    async def fire(self, subject: str, payload: dict | bytes) -> None:
        """Find a registered (possibly-wildcarded) subject that matches and invoke it."""
        cb = None
        for registered, (callback, _sub) in self.subs.items():
            if registered.endswith(".>"):
                if subject.startswith(registered[:-2] + "."):
                    cb = callback
                    break
            elif registered == subject:
                cb = callback
                break
        if cb is None:
            return  # no subscriber — fire-and-forget like a real broker
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        await cb(data, subject, "")


class _FakeSession:
    """Records send_resource_updated calls; mimics ServerSession surface."""

    def __init__(self) -> None:
        self.updated: list[str] = []

    async def send_resource_updated(self, uri) -> None:
        self.updated.append(str(uri))


@contextlib.contextmanager
def _request_ctx_with_session(session: _FakeSession):
    """Push a fake request context so manager.subscribe() can grab the session."""
    token = es.request_ctx.set(SimpleNamespace(session=session))
    try:
        yield
    finally:
        es.request_ctx.reset(token)


# ── URI helpers ───────────────────────────────────────────────────────


def test_uri_round_trip() -> None:
    assert es.uri_for_device("pi-desk") == "events://devices/pi-desk/latest"
    assert es.device_id_from_uri("events://devices/pi-desk/latest") == "pi-desk"


@pytest.mark.parametrize(
    "uri",
    [
        "events://devices//latest",          # empty device id
        "events://devices/pi/desk/latest",   # nested path → not a single id
        "events://devices/pi-desk",          # missing /latest suffix
        "http://example.com/foo",            # wrong scheme
        "",
    ],
)
def test_device_id_from_uri_rejects_garbage(uri: str) -> None:
    assert es.device_id_from_uri(uri) is None


# ── Event parsing ─────────────────────────────────────────────────────


def test_parse_event_with_jsonrpc_envelope() -> None:
    subject = "device-connect.alice.pi-desk.event.work_done"
    data = json.dumps({
        "jsonrpc": "2.0",
        "method": "work_done",
        "params": {"task_id": "T-42", "branch": "feature/T-42"},
    }).encode()
    name, params = es._parse_event(subject, data)
    assert name == "work_done"
    assert params == {"task_id": "T-42", "branch": "feature/T-42"}


def test_parse_event_with_bare_dict() -> None:
    name, params = es._parse_event(
        "device-connect.alice.pi-desk.event.progress",
        json.dumps({"step": "push"}).encode(),
    )
    assert name == "progress"
    assert params == {"step": "push"}


def test_parse_event_with_zenoh_style_separator() -> None:
    """Zenoh delivers matched keys with '/' separators; NATS uses '.'. Both must parse."""
    name, params = es._parse_event(
        "device-connect/alice/pi-desk/event/work_done",
        json.dumps({"task_id": "T-42", "branch": "feature/T-42"}).encode(),
    )
    assert name == "work_done"
    assert params == {"task_id": "T-42", "branch": "feature/T-42"}


def test_parse_event_with_garbage_bytes() -> None:
    name, params = es._parse_event(
        "device-connect.alice.pi-desk.event.weird",
        b"\x00\x01not-json",
    )
    assert name == "weird"
    assert "raw" in params


# ── Manager behavior ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subscribe_then_event_notifies_session() -> None:
    msg = _FakeMessagingClient()
    mgr = es.EventSubscriptionManager(msg, tenant="alice")
    session = _FakeSession()
    uri = es.uri_for_device("pi-desk")

    with _request_ctx_with_session(session):
        await mgr.subscribe(uri)

    # One fabric subscription was created with the right subject pattern.
    assert "device-connect.alice.pi-desk.event.>" in msg.subs

    await msg.fire(
        "device-connect.alice.pi-desk.event.work_done",
        {"jsonrpc": "2.0", "method": "work_done",
         "params": {"task_id": "T-42", "branch": "feature/T-42"}},
    )
    # Give the callback a tick to run if it scheduled anything.
    await asyncio.sleep(0)

    assert session.updated == [uri]
    # And the latest event is now readable.
    payload = json.loads(await mgr.read("pi-desk"))
    assert payload["device_id"] == "pi-desk"
    assert payload["event_name"] == "work_done"
    assert payload["params"]["task_id"] == "T-42"


@pytest.mark.asyncio
async def test_two_sessions_one_device_share_a_fabric_sub() -> None:
    msg = _FakeMessagingClient()
    mgr = es.EventSubscriptionManager(msg, tenant="alice")
    s1, s2 = _FakeSession(), _FakeSession()
    uri = es.uri_for_device("pi-desk")

    with _request_ctx_with_session(s1):
        await mgr.subscribe(uri)
    with _request_ctx_with_session(s2):
        await mgr.subscribe(uri)

    # Still only one fabric subscription.
    assert len(msg.subs) == 1

    await msg.fire(
        "device-connect.alice.pi-desk.event.progress",
        {"step": "agent"},
    )
    await asyncio.sleep(0)

    assert s1.updated == [uri]
    assert s2.updated == [uri]


@pytest.mark.asyncio
async def test_unsubscribe_releases_fabric_sub_when_last_subscriber_leaves() -> None:
    msg = _FakeMessagingClient()
    mgr = es.EventSubscriptionManager(msg, tenant="alice")
    s1, s2 = _FakeSession(), _FakeSession()
    uri = es.uri_for_device("pi-desk")

    with _request_ctx_with_session(s1):
        await mgr.subscribe(uri)
    with _request_ctx_with_session(s2):
        await mgr.subscribe(uri)

    fabric = msg.subs["device-connect.alice.pi-desk.event.>"][1]

    # First unsubscribe: fabric sub stays alive.
    with _request_ctx_with_session(s1):
        await mgr.unsubscribe(uri)
    assert not fabric.unsubscribed

    # Second unsubscribe: fabric sub is torn down.
    with _request_ctx_with_session(s2):
        await mgr.unsubscribe(uri)
    assert fabric.unsubscribed

    # Subsequent events reach no one.
    await msg.fire(
        "device-connect.alice.pi-desk.event.work_done",
        {"step": "push"},
    )
    await asyncio.sleep(0)
    assert s1.updated == []
    assert s2.updated == []


@pytest.mark.asyncio
async def test_read_returns_empty_payload_when_no_events_yet() -> None:
    mgr = es.EventSubscriptionManager(_FakeMessagingClient(), tenant="alice")
    payload = json.loads(await mgr.read("pi-desk"))
    assert payload == {"device_id": "pi-desk", "event_name": None, "params": {}}


@pytest.mark.asyncio
async def test_subscribe_to_unrelated_uri_is_a_noop() -> None:
    msg = _FakeMessagingClient()
    mgr = es.EventSubscriptionManager(msg, tenant="alice")
    session = _FakeSession()

    with _request_ctx_with_session(session):
        await mgr.subscribe("file:///something/else")

    assert msg.subs == {}
    assert session.updated == []
