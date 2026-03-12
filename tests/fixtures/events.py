"""Event capture and assertion utilities for cross-repo integration tests.

Uses the SDK MessagingClient abstraction — supports NATS, Zenoh, and MQTT backends.
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional

from device_connect_sdk.messaging import create_client

logger = logging.getLogger(__name__)


@dataclass
class CapturedEvent:
    """A captured messaging event."""

    subject: str
    data: dict
    timestamp: datetime
    device_id: Optional[str] = None
    event_name: Optional[str] = None
    raw: bytes = field(default=b"", repr=False)

    @classmethod
    def from_message(cls, subject: str, data: bytes) -> "CapturedEvent":
        try:
            payload = json.loads(data)
            event_data = payload.get("params", payload)
        except json.JSONDecodeError:
            event_data = {"raw": data.decode(errors="replace")}

        parts = subject.split(".")
        device_id = parts[2] if len(parts) > 2 else None
        event_name = ".".join(parts[4:]) if len(parts) > 4 else None

        return cls(
            subject=subject,
            data=event_data,
            timestamp=datetime.now(),
            device_id=device_id,
            event_name=event_name,
            raw=data,
        )

    def matches(
        self,
        event_name: Optional[str] = None,
        device_id: Optional[str] = None,
        predicate: Optional[Callable[["CapturedEvent"], bool]] = None,
    ) -> bool:
        if event_name and self.event_name != event_name:
            return False
        if device_id and self.device_id != device_id:
            return False
        if predicate and not predicate(self):
            return False
        return True


class EventStream:
    """Stream of captured events with wait/assert methods."""

    def __init__(self):
        self._events: List[CapturedEvent] = []
        self._waiters: list[tuple] = []
        self._lock = asyncio.Lock()

    async def add(self, event: CapturedEvent) -> None:
        async with self._lock:
            self._events.append(event)
            logger.debug(f"Captured: {event.event_name} from {event.device_id}")
            for predicate, future in list(self._waiters):
                if not future.done() and predicate(event):
                    future.set_result(event)
                    self._waiters.remove((predicate, future))

    async def wait_for(
        self,
        event_name: Optional[str] = None,
        device_id: Optional[str] = None,
        predicate: Optional[Callable[[CapturedEvent], bool]] = None,
        timeout: float = 10.0,
    ) -> CapturedEvent:
        def matches(e: CapturedEvent) -> bool:
            return e.matches(event_name, device_id, predicate)

        async with self._lock:
            for event in self._events:
                if matches(event):
                    return event
            future: asyncio.Future[CapturedEvent] = asyncio.Future()
            self._waiters.append((matches, future))

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            async with self._lock:
                self._waiters = [(p, f) for p, f in self._waiters if f is not future]
            raise TimeoutError(
                f"Timeout waiting for event: event_name={event_name}, device_id={device_id}"
            )

    def get_events(
        self, event_name: Optional[str] = None, device_id: Optional[str] = None,
    ) -> List[CapturedEvent]:
        return [e for e in self._events if e.matches(event_name, device_id)]

    def count(self, event_name: Optional[str] = None, device_id: Optional[str] = None) -> int:
        return len(self.get_events(event_name, device_id))

    def clear(self) -> None:
        self._events.clear()


class EventCollector:
    """Collects events from messaging backend for testing.

    Usage:
        async with EventCollector(backend="nats", url="nats://localhost:4222") as collector:
            async with collector.subscribe("device-connect.*.*.event.*") as events:
                event = await events.wait_for("state_change_detected")
    """

    def __init__(self, backend: str, url: str):
        self.backend = backend
        self.url = url
        self._messaging = None

    async def __aenter__(self) -> "EventCollector":
        self._messaging = create_client(self.backend)
        await self._messaging.connect(servers=[self.url])
        return self

    async def __aexit__(self, *args) -> None:
        if self._messaging:
            await self._messaging.close()

    @asynccontextmanager
    async def subscribe(self, subject: str):
        if not self._messaging:
            raise RuntimeError("EventCollector not connected")

        stream = EventStream()

        async def handler(data: bytes, subject: str, reply: Optional[str]) -> None:
            # Normalize Zenoh slash-separated subjects to dot-separated
            normalized = subject.replace("/", ".")
            event = CapturedEvent.from_message(normalized, data)
            await stream.add(event)

        sub = await self._messaging.subscribe_with_subject(subject, handler)
        try:
            yield stream
        finally:
            await sub.unsubscribe()
