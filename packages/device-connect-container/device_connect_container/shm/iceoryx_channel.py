"""iceoryx2 IPC channel — sub-microsecond zero-copy for same-host communication.

Alternative to Zenoh SHM for capability pairs that need the lowest
possible latency (sub-100ns vs Zenoh SHM's ~5μs).

iceoryx2 is a fully decentralized (no broker), lock-free, zero-copy IPC
middleware. It communicates only on the same host — for cross-device
communication, use Zenoh (which can bridge to the network).

Select via manifest.json:
    {
        "container": {
            "ipc": "iceoryx2"
        }
    }

Requires: pip install iceoryx2>=0.8.0
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable, Generic, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Lazy import to avoid hard dependency
_iceoryx2 = None


def _get_iceoryx2():
    global _iceoryx2
    if _iceoryx2 is None:
        try:
            import iceoryx2
            _iceoryx2 = iceoryx2
        except ImportError:
            raise ImportError(
                "iceoryx2 IPC requires the iceoryx2 package. "
                "Install with: pip install iceoryx2>=0.8.0"
            )
    return _iceoryx2


class IceoryxChannel(Generic[T]):
    """Zero-copy IPC channel using iceoryx2.

    Drop-in replacement for ShmChannel when ultra-low-latency same-host
    communication is needed. Uses iceoryx2's publish/subscribe pattern
    with lock-free shared memory.

    Usage:
        channel = IceoryxChannel(service_name="cam-001-video")

        # Publisher
        await channel.initialize_writer()
        await channel.write(frame_bytes)

        # Subscriber
        await channel.subscribe(on_frame)
    """

    def __init__(
        self,
        service_name: str,
        codec: Optional[Any] = None,
        max_publishers: int = 1,
        max_subscribers: int = 8,
        history_size: int = 1,
    ):
        """
        Args:
            service_name: iceoryx2 service name (unique per data stream).
            codec: Serialization codec. If None, data is raw bytes.
            max_publishers: Maximum concurrent publishers.
            max_subscribers: Maximum concurrent subscribers.
            history_size: Number of past samples cached for late joiners.
        """
        self._service_name = service_name
        self._codec = codec
        self._max_publishers = max_publishers
        self._max_subscribers = max_subscribers
        self._history_size = history_size
        self._node = None
        self._service = None
        self._publisher = None
        self._subscriber = None

    async def initialize_writer(self, **kwargs) -> None:
        """Initialize as a publisher.

        Creates an iceoryx2 node and publish/subscribe service.
        """
        iox2 = _get_iceoryx2()

        self._node = iox2.Node.new(f"dc-pub-{self._service_name}").create()

        self._service = (
            self._node
            .service_builder(self._service_name)
            .publish_subscribe()
            .max_publishers(self._max_publishers)
            .max_subscribers(self._max_subscribers)
            .history_size(self._history_size)
            .open_or_create()
        )

        self._publisher = self._service.publisher_builder().create()

        logger.info(
            "IceoryxChannel publisher initialized: %s", self._service_name
        )

    async def write(self, data: Any) -> None:
        """Publish data via iceoryx2 (zero-copy).

        Args:
            data: Data to publish. Serialized via codec if set.
        """
        if self._publisher is None:
            raise RuntimeError("Writer not initialized.")

        if self._codec is not None:
            raw = self._codec.encode(data)
        elif isinstance(data, (bytes, bytearray)):
            raw = bytes(data)
        else:
            raise TypeError(f"Without codec, data must be bytes, got {type(data)}")

        # iceoryx2: loan a sample, fill it, send it (zero-copy)
        sample = self._publisher.loan_uninit()
        payload = sample.payload_mut()
        payload[:len(raw)] = raw
        sample.send()

    async def subscribe(
        self,
        callback: Callable[[str, Any], Awaitable[None]],
    ) -> None:
        """Subscribe to the iceoryx2 service.

        Args:
            callback: Async callback(service_name, data).
        """
        iox2 = _get_iceoryx2()

        if self._node is None:
            self._node = iox2.Node.new(f"dc-sub-{self._service_name}").create()

        if self._service is None:
            self._service = (
                self._node
                .service_builder(self._service_name)
                .publish_subscribe()
                .open_or_create()
            )

        self._subscriber = self._service.subscriber_builder().create()

        # Start polling loop
        asyncio.create_task(self._poll_loop(callback))

        logger.info(
            "IceoryxChannel subscriber started: %s", self._service_name
        )

    async def _poll_loop(
        self, callback: Callable[[str, Any], Awaitable[None]],
    ) -> None:
        """Poll for new samples from iceoryx2."""
        while self._subscriber is not None:
            try:
                sample = self._subscriber.receive()
                if sample is not None:
                    payload = sample.payload()
                    if self._codec is not None:
                        data = self._codec.decode(memoryview(payload))
                    else:
                        data = memoryview(payload)
                    await callback(self._service_name, data)
                else:
                    # No sample available, yield control briefly
                    await asyncio.sleep(0.0001)  # 100μs poll interval
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("IceoryxChannel poll error: %s", e)
                await asyncio.sleep(0.001)

    async def read(self, timeout: float = 5.0) -> Any:
        """Read the next value (blocking).

        Args:
            timeout: Maximum seconds to wait.

        Returns:
            Data value.
        """
        if self._subscriber is None:
            raise RuntimeError("Not subscribed.")

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            sample = self._subscriber.receive()
            if sample is not None:
                payload = sample.payload()
                if self._codec is not None:
                    return self._codec.decode(memoryview(payload))
                return memoryview(payload)
            await asyncio.sleep(0.0001)

        raise asyncio.TimeoutError(f"No data received within {timeout}s")

    async def close(self) -> None:
        """Close the channel."""
        self._publisher = None
        self._subscriber = None
        self._service = None
        self._node = None
        logger.debug("IceoryxChannel closed: %s", self._service_name)
