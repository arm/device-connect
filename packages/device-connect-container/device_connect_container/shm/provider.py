"""SHM data provider — wraps Zenoh SHM for zero-copy publishing.

Allocates shared memory buffers via Zenoh's ShmProvider and publishes
data without kernel-space copies. Co-located subscribers receive a
memoryview directly into the shared segment.

Requires:
    - eclipse-zenoh built with shared-memory feature
    - Containers sharing /dev/shm (via --ipc=host or shared volume)
"""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ShmDataProvider:
    """Zero-copy shared memory publisher using Zenoh SHM.

    Wraps Zenoh's ShmProvider to allocate buffers in POSIX shared memory
    and publish them without copying through the kernel.

    Usage:
        provider = ShmDataProvider(zenoh_session, segment_size=64*1024*1024)
        await provider.initialize()

        # Publish frame data via SHM
        await provider.publish("device-connect.default.cam-001.data.video", frame_bytes)
    """

    def __init__(
        self,
        session: Any,
        segment_size: int = 64 * 1024 * 1024,  # 64 MB default
    ):
        """
        Args:
            session: Zenoh session (must have SHM feature enabled).
            segment_size: Total SHM segment size in bytes.
        """
        self._session = session
        self._segment_size = segment_size
        self._provider = None
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the SHM provider.

        Creates the POSIX shared memory segment and Zenoh SHM provider.

        Raises:
            ImportError: If Zenoh was not built with SHM support.
            RuntimeError: If SHM initialization fails.
        """
        try:
            import zenoh
            if not hasattr(zenoh, 'shm') or not hasattr(zenoh.shm, 'ShmProvider'):
                raise ImportError(
                    "Zenoh SHM not available. Rebuild with: "
                    "pip install eclipse-zenoh --no-binary :all: "
                    "--config-settings build-args='--features=zenoh/shared-memory'"
                )

            self._provider = zenoh.shm.ShmProvider.default_backend(self._segment_size)
            self._initialized = True
            logger.info(
                "SHM provider initialized (segment_size=%d bytes)",
                self._segment_size,
            )
        except ImportError:
            raise
        except Exception as e:
            raise RuntimeError(f"Failed to initialize SHM provider: {e}") from e

    async def publish(self, key_expr: str, data: bytes) -> None:
        """Publish data via shared memory (zero-copy for local subscribers).

        Allocates a buffer in the SHM segment, copies the data in, and
        publishes a reference. Local subscribers receive a memoryview
        without additional copies.

        Args:
            key_expr: Zenoh key expression (topic).
            data: Raw bytes to publish.

        Raises:
            RuntimeError: If provider not initialized or allocation fails.
        """
        if not self._initialized or self._provider is None:
            raise RuntimeError("SHM provider not initialized. Call initialize() first.")

        try:
            import zenoh

            # Allocate SHM buffer with blocking garbage collection policy
            sbuf = self._provider.alloc(
                len(data),
                policy=zenoh.shm.BlockOn(zenoh.shm.GarbageCollect()),
            )
            # Copy data into the shared buffer
            sbuf[:] = data

            # Publish the SHM buffer reference (subscribers get zero-copy access)
            self._session.put(key_expr, sbuf)

        except Exception as e:
            logger.error("SHM publish failed for %s: %s", key_expr, e)
            raise

    async def publish_prealloc(self, key_expr: str, fill_fn: Any) -> None:
        """Publish with a pre-allocated buffer filled by a callback.

        Avoids even the initial copy by letting the caller write directly
        into the SHM buffer.

        Args:
            key_expr: Zenoh key expression (topic).
            fill_fn: Callable(memoryview) -> int that fills the buffer
                and returns the number of bytes written.
        """
        if not self._initialized or self._provider is None:
            raise RuntimeError("SHM provider not initialized.")

        import zenoh

        # Allocate a generous buffer; caller reports actual size
        max_size = self._segment_size // 4  # Use at most 25% of segment
        sbuf = self._provider.alloc(
            max_size,
            policy=zenoh.shm.BlockOn(zenoh.shm.GarbageCollect()),
        )

        # Let caller fill the buffer
        written = fill_fn(memoryview(sbuf))

        # Trim to actual size if supported
        if hasattr(sbuf, 'try_resize'):
            sbuf.try_resize(written)

        self._session.put(key_expr, sbuf)

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    async def close(self) -> None:
        """Release SHM resources."""
        self._provider = None
        self._initialized = False
        logger.debug("SHM provider closed")
