"""Event injection utilities for cross-repo integration tests.

Uses the SDK MessagingClient abstraction — supports NATS, Zenoh, and MQTT backends.
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from device_connect_sdk.messaging import create_client

logger = logging.getLogger(__name__)


@dataclass
class InjectedEvent:
    subject: str
    payload: dict
    timestamp: datetime
    event_id: str


class EventInjector:
    """Inject events as if they came from a device.

    Usage:
        async with EventInjector(backend="nats", url="nats://localhost:4222") as injector:
            await injector.inject_event("camera-001", "mess_detected", {"zone": "A"})
    """

    def __init__(self, backend: str, url: str, tenant: str = "default", auto_simulate: bool = True):
        self.backend = backend
        self.url = url
        self.tenant = tenant
        self.auto_simulate = auto_simulate
        self._messaging = None
        self._injected: list[InjectedEvent] = []

    async def __aenter__(self) -> "EventInjector":
        self._messaging = create_client(self.backend)
        await self._messaging.connect(servers=[self.url])
        return self

    async def __aexit__(self, *args) -> None:
        if self._messaging:
            await self._messaging.close()

    async def inject_event(
        self,
        device_id: str,
        event_name: str,
        payload: Optional[dict] = None,
        simulated: Optional[bool] = None,
    ) -> InjectedEvent:
        if not self._messaging:
            raise RuntimeError("EventInjector not connected")

        event_payload = dict(payload) if payload else {}
        event_id = uuid.uuid4().hex[:8]
        timestamp = datetime.utcnow().isoformat() + "Z"
        event_payload.setdefault("event_id", event_id)
        event_payload.setdefault("ts", timestamp)

        if simulated if simulated is not None else self.auto_simulate:
            event_payload["simulated"] = True

        subject = f"device-connect.{self.tenant}.{device_id}.event.{event_name}"
        message = {"jsonrpc": "2.0", "method": event_name, "params": event_payload}
        await self._messaging.publish(subject, json.dumps(message).encode())

        record = InjectedEvent(subject=subject, payload=event_payload, timestamp=datetime.now(), event_id=event_id)
        self._injected.append(record)
        return record

    async def inject_rpc_response(
        self, reply_to: str, result: Any = None, error: Optional[dict] = None, request_id: str = "1",
    ) -> None:
        if not self._messaging:
            raise RuntimeError("EventInjector not connected")
        response: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error:
            response["error"] = error
        else:
            response["result"] = result if result is not None else {}
        await self._messaging.publish(reply_to, json.dumps(response).encode())
