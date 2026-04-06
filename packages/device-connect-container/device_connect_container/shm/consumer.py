"""SHM data consumer — zero-copy subscriber for Zenoh SHM data.

Subscribes to Zenoh topics and provides zero-copy access to data
published via SHM. When the publisher and subscriber are co-located
(same /dev/shm), the data is accessed directly from shared memory
without kernel copies.

For remote publishers (across network), Zenoh transparently falls back
to byte-copy delivery — no code changes needed.
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Type alias for SHM data callback
ShmCallback = Callable[[str, memoryview], Awaitable[None]]


class ShmDataConsumer:
    """Zero-copy shared memory subscriber using Zenoh.

    Subscribes to data topics and delivers payloads as memoryview objects
    when SHM is available, or as bytes when falling back to network delivery.

    Usage:
        consumer = ShmDataConsumer(zenoh_session)

        async def on_frame(topic: str, data: memoryview):
            # data is zero-copy from SHM — no parsing overhead
            np_array = np.frombuffer(data, dtype=np.uint8)

        await consumer.subscribe("device-connect.default.cam-001.data.video", on_frame)
    """

    def __init__(self, session: Any):
        """
        Args:
            session: Zenoh session.
        """
        self._session = session
        self._subscriptions: list = []

    async def subscribe(
        self,
        key_expr: str,
        callback: ShmCallback,
    ) -> "ShmSubscription":
        """Subscribe to a data topic with zero-copy SHM delivery.

        Args:
            key_expr: Zenoh key expression (topic) or pattern.
            callback: Async callback(topic: str, data: memoryview).

        Returns:
            Subscription handle for unsubscription.
        """
        import zenoh

        def _on_sample(sample):
            """Bridge Zenoh sample to async callback."""
            try:
                key = str(sample.key_expr)
                # Access payload — Zenoh provides memoryview for SHM,
                # bytes for network. We normalize to memoryview.
                payload = sample.payload
                if isinstance(payload, memoryview):
                    data = payload
                elif isinstance(payload, (bytes, bytearray)):
                    data = memoryview(payload)
                else:
                    data = memoryview(bytes(payload))

                # Schedule the async callback
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(callback(key, data))
            except Exception as e:
                logger.error("Error in SHM subscriber callback: %s", e)

        sub = self._session.declare_subscriber(key_expr, _on_sample)
        subscription = ShmSubscription(sub, key_expr)
        self._subscriptions.append(subscription)

        logger.debug("SHM consumer subscribed to %s", key_expr)
        return subscription

    async def close(self) -> None:
        """Unsubscribe from all topics."""
        for sub in self._subscriptions:
            await sub.unsubscribe()
        self._subscriptions.clear()


class ShmSubscription:
    """Handle for a SHM data subscription."""

    def __init__(self, zenoh_sub: Any, key_expr: str):
        self._sub = zenoh_sub
        self._key_expr = key_expr

    async def unsubscribe(self) -> None:
        """Unsubscribe from the topic."""
        if self._sub is not None:
            self._sub.undeclare()
            self._sub = None
            logger.debug("SHM consumer unsubscribed from %s", self._key_expr)
