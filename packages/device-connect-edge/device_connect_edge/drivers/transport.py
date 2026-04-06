"""Lightweight transport wrapper for hardware-native topic access.

Exposes raw publish/subscribe on arbitrary topics using the device's
managed messaging session (NATS, Zenoh, MQTT).  This allows drivers
like ReachyMiniDriver to talk to hardware-native Zenoh topics without
maintaining their own session pool.
"""

import logging
from typing import Any, Callable, Awaitable, Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from device_connect_edge.messaging.base import MessagingClient, Subscription

logger = logging.getLogger(__name__)


class DriverTransport:
    """Raw messaging transport for DeviceDriver.

    Allows drivers to publish/subscribe to hardware-native topics
    (e.g., ``reachy_mini/command``) using Device Connect's managed connection.
    """

    def __init__(self, messaging: "MessagingClient"):
        self._messaging = messaging
        self._subscriptions: List[Any] = []

    async def publish(self, topic: str, data: bytes) -> None:
        """Publish raw bytes to a topic."""
        await self._messaging.publish(topic, data)

    async def subscribe(
        self,
        topic: str,
        callback: Callable[[bytes, Optional[str]], Awaitable[None]],
    ) -> "Subscription":
        """Subscribe to a topic with a callback.

        Args:
            topic: Topic / key expression (backend-native format)
            callback: ``async def cb(data: bytes, reply: Optional[str])``

        Returns:
            Subscription handle (kept for teardown)
        """
        sub = await self._messaging.subscribe(topic, callback)
        self._subscriptions.append(sub)
        return sub

    async def request(
        self, topic: str, data: bytes, timeout: float = 5.0,
    ) -> bytes:
        """Send a request and wait for a reply."""
        return await self._messaging.request(topic, data, timeout=timeout)

    async def create_shm_channel(
        self,
        topic: str,
        codec: Optional[Any] = None,
        segment_size: int = 64 * 1024 * 1024,
    ) -> Any:
        """Create a typed SHM channel for zero-copy data transfer.

        Requires the device-connect-container package and a Zenoh backend
        with SHM support.

        Args:
            topic: Zenoh key expression for the data stream.
            codec: Serialization codec (e.g., FlatBuffersCodec).
                If None, data is raw bytes.
            segment_size: SHM segment size in bytes (default 64MB).

        Returns:
            ShmChannel instance (from device_connect_container.shm).

        Raises:
            ImportError: If device-connect-container is not installed.
        """
        try:
            from device_connect_container.shm.channel import ShmChannel
        except ImportError:
            raise ImportError(
                "SHM channels require the device-connect-container package. "
                "Install with: pip install device-connect-container[shm]"
            )

        # Get the underlying Zenoh session from the adapter
        session = getattr(self._messaging, '_session', None)
        if session is None:
            raise RuntimeError(
                "SHM channels require a Zenoh messaging backend with an active session."
            )

        channel = ShmChannel(session=session, key_expr=topic, codec=codec)
        return channel

    async def teardown(self) -> None:
        """Unsubscribe all subscriptions created via this transport."""
        for sub in self._subscriptions:
            try:
                await sub.unsubscribe()
            except Exception:
                logger.debug("cleanup error during transport teardown unsubscribe", exc_info=True)
        self._subscriptions.clear()
