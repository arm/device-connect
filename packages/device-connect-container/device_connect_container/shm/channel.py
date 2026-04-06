"""Typed SHM channel — high-level zero-copy data transfer abstraction.

Provides a Generic[T] channel that combines SHM transport with FlatBuffers
serialization for fully zero-copy data flow between capabilities.

Data plane flow:
    Publisher: Python object → FlatBuffers serialize → SHM buffer → Zenoh put
    Subscriber: Zenoh sample → SHM memoryview → FlatBuffers accessor (no parse)

Control plane (RPC/events) continues to use JSON-RPC over standard Zenoh.
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable, Generic, Optional, TypeVar

from device_connect_container.shm.provider import ShmDataProvider
from device_connect_container.shm.consumer import ShmDataConsumer

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ShmChannel(Generic[T]):
    """Typed zero-copy shared memory channel between capabilities.

    Combines Zenoh SHM transport with optional FlatBuffers serialization
    for zero-copy end-to-end data flow.

    Data plane topics follow the pattern:
        device-connect.{tenant}.{device_id}.data.{stream_name}

    Usage:
        # Publisher side (e.g., camera capability)
        channel = ShmChannel(
            session=zenoh_session,
            key_expr="device-connect.default.cam-001.data.video",
            codec=flatbuf_codec,  # or None for raw bytes
        )
        await channel.initialize_writer(segment_size=128*1024*1024)
        await channel.write(frame_bytes)

        # Subscriber side (e.g., vision capability)
        channel = ShmChannel(
            session=zenoh_session,
            key_expr="device-connect.default.cam-001.data.video",
            codec=flatbuf_codec,
        )
        await channel.subscribe(on_frame_callback)
    """

    def __init__(
        self,
        session: Any,
        key_expr: str,
        codec: Optional[Any] = None,
    ):
        """
        Args:
            session: Zenoh session.
            key_expr: Zenoh key expression for this data stream.
            codec: Serialization codec (e.g., FlatBuffersCodec).
                If None, data is published as raw bytes.
        """
        self._session = session
        self._key_expr = key_expr
        self._codec = codec
        self._provider: Optional[ShmDataProvider] = None
        self._consumer: Optional[ShmDataConsumer] = None
        self._latest: Optional[memoryview] = None
        self._latest_event = asyncio.Event()

    async def initialize_writer(
        self,
        segment_size: int = 64 * 1024 * 1024,
    ) -> None:
        """Initialize this channel as a writer (publisher).

        Args:
            segment_size: SHM segment size in bytes.
        """
        self._provider = ShmDataProvider(self._session, segment_size)
        await self._provider.initialize()
        logger.info("ShmChannel writer initialized: %s", self._key_expr)

    async def write(self, data: Any) -> None:
        """Write data to the channel.

        If a codec is configured, data is serialized via FlatBuffers
        into the SHM buffer. Otherwise, data must be bytes.

        Args:
            data: Data to publish. Type depends on codec configuration.

        Raises:
            RuntimeError: If writer not initialized.
        """
        if self._provider is None:
            raise RuntimeError("Channel writer not initialized. Call initialize_writer() first.")

        if self._codec is not None:
            # Serialize via codec (FlatBuffers → bytes)
            raw = self._codec.encode(data)
        elif isinstance(data, (bytes, bytearray)):
            raw = bytes(data)
        else:
            raise TypeError(f"Without codec, data must be bytes, got {type(data)}")

        await self._provider.publish(self._key_expr, raw)

    async def subscribe(
        self,
        callback: Callable[[str, Any], Awaitable[None]],
    ) -> None:
        """Subscribe to this channel's data stream.

        Args:
            callback: Async callback(key_expr: str, data: T_or_memoryview).
                If codec is set, data is decoded; otherwise raw memoryview.
        """
        self._consumer = ShmDataConsumer(self._session)

        async def _on_data(key: str, raw: memoryview) -> None:
            if self._codec is not None:
                data = self._codec.decode(raw)
            else:
                data = raw
            # Store latest value for read()
            self._latest = raw
            self._latest_event.set()
            await callback(key, data)

        await self._consumer.subscribe(self._key_expr, _on_data)
        logger.info("ShmChannel subscriber started: %s", self._key_expr)

    async def read(self, timeout: float = 5.0) -> Any:
        """Read the latest value from the channel (blocking).

        Waits for at least one value to arrive, then returns it.

        Args:
            timeout: Maximum seconds to wait.

        Returns:
            Latest data value (decoded if codec is set).

        Raises:
            asyncio.TimeoutError: If no data received within timeout.
        """
        if self._consumer is None:
            raise RuntimeError("Channel not subscribed. Call subscribe() first.")

        await asyncio.wait_for(self._latest_event.wait(), timeout=timeout)

        if self._codec is not None and self._latest is not None:
            return self._codec.decode(self._latest)
        return self._latest

    async def close(self) -> None:
        """Close the channel and release resources."""
        if self._provider:
            await self._provider.close()
            self._provider = None
        if self._consumer:
            await self._consumer.close()
            self._consumer = None
        logger.debug("ShmChannel closed: %s", self._key_expr)
