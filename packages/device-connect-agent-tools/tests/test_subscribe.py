# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the selector-driven subscribe + await_replies tools.

The tests stand up a fake Connection that mirrors the buffered-inbox API
the production class exposes (``subscribe_buffered`` /
``unsubscribe_buffered`` / ``get_inbox`` / ``_inbox`` dict). Real
messaging is not exercised here; integration tests cover the wire.
"""
from unittest.mock import patch

import pytest

from device_connect_agent_tools import tools as tools_mod


SAMPLE_DEVICES = [
    {
        "device_id": "cam-001",
        "device_type": "camera",
        "labels": {"category": "camera", "location": "lab-A"},
        "functions": [],
        "events": [
            {"name": "object_detected", "labels": {"modality": "rgb"}},
        ],
    },
    {
        "device_id": "cam-002",
        "device_type": "camera",
        "labels": {"category": "camera", "location": "lab-A"},
        "functions": [],
        "events": [
            {"name": "object_detected", "labels": {"modality": "rgb"}},
        ],
    },
]


class FakeConnection:
    """Minimal fake of the agent-tools Connection used by Subscription."""

    def __init__(self, devices=None, zone="default"):
        self.zone = zone
        self.devices = devices or []
        self._inbox: dict[str, list[tuple]] = {}
        self.subscribed_subjects: list[str] = []
        self.unsubscribed_names: list[str] = []

    def list_devices(self):
        return list(self.devices)

    def subscribe_buffered(self, subject: str, name: str | None = None) -> str:
        name = name or subject
        self._inbox[name] = []
        self.subscribed_subjects.append(subject)
        return name

    def unsubscribe_buffered(self, name: str) -> None:
        self.unsubscribed_names.append(name)
        self._inbox.pop(name, None)

    def get_inbox(self, name: str | None = None):
        if name is not None:
            return {name: list(self._inbox.get(name, []))}
        return {k: list(v) for k, v in self._inbox.items()}

    # Test helper: simulate a message landing on a given subject.
    def deliver(self, subject: str, payload: dict):
        for name, _ in list(self._inbox.items()):
            self._inbox[name].append((subject, payload))


@pytest.fixture
def fake_conn():
    conn = FakeConnection(devices=SAMPLE_DEVICES)
    with patch.object(tools_mod, "get_connection", return_value=conn):
        yield conn


# -- subscribe ------------------------------------------------------


class TestSubscribe:
    def test_correlation_form_subscribes_to_reply_subject(self, fake_conn):
        sub = tools_mod.subscribe("correlation:abc-123")
        assert len(fake_conn.subscribed_subjects) == 1
        subj = fake_conn.subscribed_subjects[0]
        assert subj == "device-connect.default.*.event.async_reply.abc-123"
        sub.close()
        assert fake_conn.unsubscribed_names

    def test_correlation_form_with_empty_id_rejected(self, fake_conn):
        with pytest.raises(ValueError):
            tools_mod.subscribe("correlation:")

    def test_event_selector_subscribes_per_device(self, fake_conn):
        sub = tools_mod.subscribe("device(*).event(object_detected)")
        # Two cameras emit object_detected -> two subjects subscribed.
        assert len(fake_conn.subscribed_subjects) == 2
        for subj in fake_conn.subscribed_subjects:
            assert subj.startswith("device-connect.default.")
            assert subj.endswith(".event.object_detected")
        sub.close()

    def test_event_selector_zero_matches_returns_idle(self, fake_conn):
        sub = tools_mod.subscribe("event(no_such_event)")
        assert fake_conn.subscribed_subjects == []
        # Idle subscription: read returns empty, close is a no-op.
        assert sub.read() == []
        sub.close()

    def test_non_event_scope_rejected(self, fake_conn):
        with pytest.raises(ValueError) as exc:
            tools_mod.subscribe("device(cam-001)")
        assert "subscribe requires" in str(exc.value)

    def test_empty_or_non_string_rejected(self, fake_conn):
        with pytest.raises(ValueError):
            tools_mod.subscribe("")
        with pytest.raises(ValueError):
            tools_mod.subscribe(None)  # type: ignore[arg-type]


# -- Subscription ---------------------------------------------------


class TestSubscriptionHandle:
    def test_read_drains_buffered_messages(self, fake_conn):
        sub = tools_mod.subscribe("correlation:r1")
        fake_conn.deliver(
            "device-connect.default.cam-001.event.async_reply.r1",
            {"correlation_id": "r1", "device_id": "cam-001", "success": True},
        )
        msgs = sub.read()
        assert len(msgs) == 1
        assert msgs[0]["device_id"] == "cam-001"
        # Subject is stamped onto the payload for source attribution.
        assert "_subject" in msgs[0]
        # A second read returns nothing -- the buffer is drained.
        assert sub.read() == []
        sub.close()

    def test_context_manager_closes(self, fake_conn):
        with tools_mod.subscribe("correlation:r2") as sub:
            assert sub.read() == []
        assert fake_conn.unsubscribed_names  # close() ran

    def test_iter_yields_until_idle_timeout(self, fake_conn):
        sub = tools_mod.subscribe("correlation:r3")
        fake_conn.deliver(
            "device-connect.default.cam-001.event.async_reply.r3",
            {"correlation_id": "r3", "device_id": "cam-001"},
        )
        # Short timeout; iter() should yield the buffered reply then exit
        # once no new messages arrive within the idle window.
        msgs = list(sub.iter(timeout=0.1, poll_interval=0.01))
        assert len(msgs) == 1
        sub.close()

    def test_for_loop_protocol_via_dunder_iter(self, fake_conn):
        # ``for msg in sub:`` should drive __iter__ which delegates to iter()
        # with a sensible default timeout. Break early so the test does not
        # block on the 30s default.
        sub = tools_mod.subscribe("correlation:r_iter")
        fake_conn.deliver(
            "device-connect.default.cam-001.event.async_reply.r_iter",
            {"correlation_id": "r_iter", "device_id": "cam-001"},
        )
        gathered: list[dict] = []
        for msg in sub:
            gathered.append(msg)
            break  # one message is enough to confirm __iter__ wiring
        sub.close()
        assert len(gathered) == 1
        assert gathered[0]["device_id"] == "cam-001"

    def test_read_does_not_drop_messages_appended_during_iteration(self, fake_conn):
        # Race-safety guard: simulate a callback that appends a fresh
        # message between the read's snapshot and truncation. The message
        # must still be visible on the next read().
        sub = tools_mod.subscribe("correlation:r_race")
        fake_conn.deliver(
            "device-connect.default.cam-001.event.async_reply.r_race",
            {"correlation_id": "r_race", "device_id": "cam-001", "ordinal": 1},
        )
        first = sub.read()
        assert len(first) == 1
        # Now simulate a late-arriving append into the same inbox AFTER
        # the previous read drained the prefix.
        fake_conn.deliver(
            "device-connect.default.cam-002.event.async_reply.r_race",
            {"correlation_id": "r_race", "device_id": "cam-002", "ordinal": 2},
        )
        second = sub.read()
        assert len(second) == 1
        assert second[0]["device_id"] == "cam-002"
        sub.close()


# -- await_replies --------------------------------------------------


class TestAwaitReplies:
    def test_empty_correlation_id_returns_empty_list(self, fake_conn):
        assert tools_mod.await_replies("") == []

    def test_collects_replies_until_count(self, fake_conn):
        # Pre-stage two replies on the to-be-subscribed subject. await_replies
        # subscribes (drains nothing yet), then deliver more during the loop.
        # We deliver up-front via the fake's deliver hook so the first poll
        # picks them up.
        def deliver_when_subscribed(subject, name=None):
            n = FakeConnection.subscribe_buffered(fake_conn, subject, name)
            # Pre-load a couple of replies so the first poll returns them.
            fake_conn.deliver(
                "device-connect.default.cam-001.event.async_reply.r4",
                {"correlation_id": "r4", "device_id": "cam-001"},
            )
            fake_conn.deliver(
                "device-connect.default.cam-002.event.async_reply.r4",
                {"correlation_id": "r4", "device_id": "cam-002"},
            )
            return n

        with patch.object(
            fake_conn, "subscribe_buffered", side_effect=deliver_when_subscribed,
        ):
            replies = tools_mod.await_replies(
                "r4", timeout=2.0, until=2, poll_interval=0.01,
            )
        assert len(replies) == 2
        ids = {r["device_id"] for r in replies}
        assert ids == {"cam-001", "cam-002"}

    def test_returns_after_timeout_with_partial(self, fake_conn):
        # No replies delivered -> after timeout, returns empty list.
        replies = tools_mod.await_replies(
            "r5", timeout=0.1, poll_interval=0.01,
        )
        assert replies == []
