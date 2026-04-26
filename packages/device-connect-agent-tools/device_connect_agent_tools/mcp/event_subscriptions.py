# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Bridge Device Connect events to MCP resource subscriptions.

The MCP bridge exposes a resource template at
``events://devices/{device_id}/latest``. Clients subscribe to that URI via
``resources/subscribe``; whenever a Device Connect event arrives on
``device-connect.{tenant}.{device_id}.event.>`` the manager pushes a
``notifications/resources/updated`` to the subscribed session(s) and the
client can re-read the resource to get the payload.

This is the asynchronous push path — the MCP client does not need to call a
blocking tool; the dispatcher LLM can keep doing other work and pick up the
event between turns.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from mcp.server.lowlevel.server import request_ctx
from mcp.server.session import ServerSession
from pydantic import AnyUrl

logger = logging.getLogger(__name__)

_URI_PREFIX = "events://devices/"
_URI_SUFFIX = "/latest"


def device_id_from_uri(uri: str) -> Optional[str]:
    """Extract device_id from ``events://devices/{device_id}/latest``.

    Returns None if *uri* does not match the expected shape. We parse by
    string ops rather than urllib to keep the URI scheme custom-friendly.
    """
    if not uri.startswith(_URI_PREFIX) or not uri.endswith(_URI_SUFFIX):
        return None
    middle = uri[len(_URI_PREFIX): -len(_URI_SUFFIX)]
    if not middle or "/" in middle:
        return None
    return middle


def uri_for_device(device_id: str) -> str:
    return f"{_URI_PREFIX}{device_id}{_URI_SUFFIX}"


def _parse_event(subject: str, data: bytes) -> tuple[str, dict[str, Any]]:
    """Pull (event_name, params) out of a Device Connect event message.

    Zenoh delivers the matched key using '/' as the separator; NATS uses '.'.
    We normalize before splitting so both backends work.
    """
    parts = subject.replace("/", ".").split(".")
    # Expected: device-connect.{tenant}.{device}.event.{event_name}
    event_name = parts[-1] if len(parts) >= 5 else "unknown"
    try:
        payload = json.loads(data.decode())
    except Exception:
        return event_name, {"raw": data.decode("utf-8", errors="replace")[:500]}

    if isinstance(payload, dict) and "method" in payload:
        # JSON-RPC notification envelope: pull the params out
        params = payload.get("params", {})
        if not isinstance(params, dict):
            params = {"value": params}
        return event_name, params
    if isinstance(payload, dict):
        return event_name, payload
    return event_name, {"value": payload}


# How many recent events to keep per device. Sized for the dispatch→wait race
# window — the worker can fire a handful of progress events plus a terminal
# event between dispatch returning and a wait_for_event call subscribing.
_RECENT_EVENTS_LIMIT = 32


def _matches(event: dict[str, Any], event_name: str, match_params: Optional[dict[str, Any]]) -> bool:
    """Return True iff *event* satisfies the optional name + params filters."""
    if event_name and event.get("event_name") != event_name:
        return False
    if match_params:
        p = event.get("params") or {}
        if not all(p.get(k) == v for k, v in match_params.items()):
            return False
    return True


class EventSubscriptionManager:
    """Tracks MCP subscriptions and forwards Device Connect events to subscribers."""

    def __init__(self, messaging_client: Any, tenant: str) -> None:
        self._messaging = messaging_client
        self._tenant = tenant
        self._device_subs: dict[str, Any] = {}
        # device_id -> set of (session, uri_string) pairs
        self._subscribers: dict[str, set[tuple[ServerSession, str]]] = {}
        self._latest: dict[str, dict[str, Any]] = {}
        # device_id -> ring of recent events (oldest first), so wait_for_event
        # can resolve dispatch→wait races against events that already fired.
        self._recent: dict[str, list[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()
        # device_id -> list of asyncio.Queue feeding waiters (one queue per waiter)
        self._waiters: dict[str, list[asyncio.Queue]] = {}
        # device_ids whose fabric subscription is pinned (won't be torn down by
        # waiter timeouts or unsubscribes). Used to pre-warm subs on
        # invoke_device so wait_for_event catches events that fire faster than
        # the round-trip from the dispatcher.
        self._pinned: set[str] = set()

    async def subscribe(self, uri: str) -> None:
        """Handle an MCP ``resources/subscribe`` request."""
        device_id = device_id_from_uri(uri)
        if device_id is None:
            logger.warning("subscribe: unrecognized URI %s", uri)
            return
        try:
            session: ServerSession = request_ctx.get().session
        except LookupError:
            logger.error("subscribe: no active request context for %s", uri)
            return

        async with self._lock:
            self._subscribers.setdefault(device_id, set()).add((session, uri))
            if device_id not in self._device_subs:
                self._device_subs[device_id] = await self._start_fabric_sub(device_id)
        logger.info("MCP subscribe: device=%s session=%s", device_id, id(session))

    async def unsubscribe(self, uri: str) -> None:
        """Handle an MCP ``resources/unsubscribe`` request."""
        device_id = device_id_from_uri(uri)
        if device_id is None:
            return
        try:
            session: ServerSession = request_ctx.get().session
        except LookupError:
            return

        fabric_sub = None
        async with self._lock:
            subs = self._subscribers.get(device_id)
            if subs is None:
                return
            subs.discard((session, uri))
            if subs:
                return
            self._subscribers.pop(device_id, None)
            # Don't tear down the fabric sub if it's pinned (pre-warmed by
            # invoke_device) or if a wait_for_event is still listening.
            if device_id not in self._pinned and not self._waiters.get(device_id):
                fabric_sub = self._device_subs.pop(device_id, None)
        if fabric_sub is not None:
            try:
                await fabric_sub.unsubscribe()
            except Exception:
                logger.debug("error unsubscribing fabric sub", exc_info=True)
        logger.info("MCP unsubscribe: device=%s session=%s", device_id, id(session))

    async def read(self, device_id: str) -> str:
        """Return the latest event for *device_id* as a JSON string."""
        payload = self._latest.get(device_id)
        if payload is None:
            return json.dumps({"device_id": device_id, "event_name": None, "params": {}})
        return json.dumps(payload)

    async def ensure_fabric_sub(self, device_id: str) -> None:
        """Pre-warm a fabric subscription for *device_id* and pin it.

        Called by the bridge before any invoke_device so events fired
        between the RPC reply and a subsequent wait_for_event are
        captured in the ring buffer rather than lost. Pinned subs
        survive waiter/unsubscribe teardown and are released only when
        the bridge shuts down (close()).
        """
        async with self._lock:
            self._pinned.add(device_id)
            if device_id not in self._device_subs:
                self._device_subs[device_id] = await self._start_fabric_sub(device_id)

    async def wait_for_event(
        self,
        device_id: str,
        timeout: float,
        event_name: str = "",
        match_params: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """Wait for a matching event for *device_id*, or return None on timeout.

        Resolves the classic dispatch→wait race by checking the recent-event
        ring buffer first. If a matching event already fired (e.g. the task
        completed before the caller subscribed), it's returned immediately.
        Otherwise, blocks for the next matching event until *timeout*.

        Args:
            device_id: Which device to listen on.
            timeout: Seconds to wait for a future event. Past matches in the
                ring buffer return immediately regardless.
            event_name: Optional event-name filter (e.g. "work_done"). Empty
                string matches any event.
            match_params: Optional ``{key: value}`` constraints; the event's
                params must contain every key with the same value to match.
                Useful for filtering by ``task_id``.

        Returns:
            The matched event dict (``{device_id, event_name, params}``) or
            None on timeout.
        """
        # Race fix: scan recent events first (newest → oldest). Handles the
        # case where the worker fires the terminal event before the caller
        # gets to subscribe.
        async with self._lock:
            recent = list(self._recent.get(device_id, []))
        for event in reversed(recent):
            if _matches(event, event_name, match_params):
                return event

        # Ensure a fabric subscription exists for this device while we wait.
        async with self._lock:
            if device_id not in self._device_subs:
                self._device_subs[device_id] = await self._start_fabric_sub(device_id)
            queue: asyncio.Queue = asyncio.Queue()
            self._waiters.setdefault(device_id, []).append(queue)
        try:
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    return None
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    return None
                if not _matches(payload, event_name, match_params):
                    continue
                return payload
        finally:
            sub_to_release = None
            async with self._lock:
                waiters = self._waiters.get(device_id, [])
                if queue in waiters:
                    waiters.remove(queue)
                if not waiters:
                    self._waiters.pop(device_id, None)
                    # Tear down the fabric sub only if no MCP subscriber and not pinned.
                    if (
                        device_id not in self._subscribers
                        and device_id not in self._pinned
                    ):
                        sub_to_release = self._device_subs.pop(device_id, None)
            if sub_to_release is not None:
                try:
                    await sub_to_release.unsubscribe()
                except Exception:
                    logger.debug("error releasing fabric sub", exc_info=True)

    async def close(self) -> None:
        """Tear down all fabric subscriptions and clear in-memory state."""
        async with self._lock:
            subs = list(self._device_subs.values())
            self._device_subs.clear()
            self._subscribers.clear()
            self._pinned.clear()
            self._waiters.clear()
        for sub in subs:
            try:
                await sub.unsubscribe()
            except Exception:
                logger.debug("error unsubscribing during close", exc_info=True)

    # ── fabric-side ────────────────────────────────────────────────────

    async def _start_fabric_sub(self, device_id: str) -> Any:
        subject = f"device-connect.{self._tenant}.{device_id}.event.>"

        async def on_msg(data: bytes, msg_subject: str, reply: str = "") -> None:
            await self._handle_event(device_id, msg_subject, data)

        sub = await self._messaging.subscribe_with_subject(subject, callback=on_msg)
        logger.info("event-bridge subscribed to fabric subject: %s", subject)
        return sub

    async def _handle_event(self, device_id: str, subject: str, data: bytes) -> None:
        event_name, params = _parse_event(subject, data)
        payload = {"device_id": device_id, "event_name": event_name, "params": params}
        self._latest[device_id] = payload

        async with self._lock:
            ring = self._recent.setdefault(device_id, [])
            ring.append(payload)
            if len(ring) > _RECENT_EVENTS_LIMIT:
                del ring[: len(ring) - _RECENT_EVENTS_LIMIT]
            subs = list(self._subscribers.get(device_id, set()))
            waiters = list(self._waiters.get(device_id, []))
        for session, uri in subs:
            try:
                await session.send_resource_updated(AnyUrl(uri))
            except Exception:
                logger.warning(
                    "failed to push resources/updated for %s to session=%s",
                    uri, id(session), exc_info=True,
                )
        for queue in waiters:
            try:
                queue.put_nowait(payload)
            except Exception:
                logger.debug("waiter queue put failed", exc_info=True)
