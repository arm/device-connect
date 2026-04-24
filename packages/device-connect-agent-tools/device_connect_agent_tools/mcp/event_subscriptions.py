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


class EventSubscriptionManager:
    """Tracks MCP subscriptions and forwards Device Connect events to subscribers."""

    def __init__(self, messaging_client: Any, tenant: str) -> None:
        self._messaging = messaging_client
        self._tenant = tenant
        self._device_subs: dict[str, Any] = {}
        # device_id -> set of (session, uri_string) pairs
        self._subscribers: dict[str, set[tuple[ServerSession, str]]] = {}
        self._latest: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

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

        async with self._lock:
            subs = self._subscribers.get(device_id)
            if subs is None:
                return
            subs.discard((session, uri))
            if subs:
                return
            self._subscribers.pop(device_id, None)
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

    async def close(self) -> None:
        """Tear down all fabric subscriptions and clear in-memory state."""
        async with self._lock:
            subs = list(self._device_subs.values())
            self._device_subs.clear()
            self._subscribers.clear()
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
            subs = list(self._subscribers.get(device_id, set()))
        for session, uri in subs:
            try:
                await session.send_resource_updated(AnyUrl(uri))
            except Exception:
                logger.warning(
                    "failed to push resources/updated for %s to session=%s",
                    uri, id(session), exc_info=True,
                )
