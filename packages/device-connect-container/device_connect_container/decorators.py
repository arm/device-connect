"""Container-specific decorators for Device Connect capabilities.

Provides the @stream decorator for high-bandwidth data plane methods
that use SHM + FlatBuffers for zero-copy transfer between containers.

The @stream decorator is separate from the base @rpc/@emit decorators
in device-connect-edge to avoid adding SHM/FlatBuffers dependencies
to the lightweight edge package.

Usage:
    from device_connect_container.decorators import stream

    class VisionCapability:
        @stream(schema="frame", shm=True, fps=30)
        async def video_feed(self) -> AsyncIterator[dict]:
            while True:
                frame = await self.capture()
                yield frame
"""

import asyncio
import functools
import inspect
import logging
from typing import Any, AsyncIterator, Callable, Optional

logger = logging.getLogger(__name__)


def stream(
    schema: Optional[str] = None,
    shm: bool = True,
    fps: Optional[float] = None,
    description: str = "",
    key_expr_suffix: Optional[str] = None,
):
    """Decorator for high-bandwidth data stream methods.

    Marks an async generator method as a data stream that publishes
    via Zenoh SHM (or falls back to byte-copy). The decorated method
    should yield data items that are published on the data plane topic.

    Data plane topic: device-connect.{tenant}.{device_id}.data.{stream_name}

    Args:
        schema: FlatBuffers schema type name (e.g., "frame", "pointcloud").
            If None, data is published as raw bytes.
        shm: Enable SHM transport (requires shared /dev/shm).
        fps: Target publish rate in frames/second. If None, publishes
            as fast as the generator yields.
        description: Human-readable description of the stream.
        key_expr_suffix: Custom Zenoh key expression suffix. Defaults to
            the method name.

    Returns:
        Decorated method with stream metadata attached.
    """

    def decorator(func: Callable) -> Callable:
        if not inspect.isasyncgenfunction(func):
            raise TypeError(
                f"@stream requires an async generator function (async def ... yield ...), "
                f"got {type(func).__name__}"
            )

        @functools.wraps(func)
        async def wrapper(self, *args, **kwargs):
            # The wrapper is called by the sidecar runtime to start streaming.
            # It iterates the generator and publishes each yielded value.
            stream_name = key_expr_suffix or func.__name__

            # Get ShmChannel from the capability's device reference
            channel = getattr(self, "_shm_channel", None)

            interval = 1.0 / fps if fps else 0.0

            async for item in func(self, *args, **kwargs):
                if channel is not None:
                    await channel.write(item)
                else:
                    # Fallback: store in buffer for manual retrieval
                    logger.debug("Stream %s yielded item (no SHM channel)", stream_name)

                if interval > 0:
                    await asyncio.sleep(interval)

        # Attach metadata for runtime discovery
        wrapper._is_device_stream = True
        wrapper._stream_name = key_expr_suffix or func.__name__
        wrapper._stream_schema = schema
        wrapper._stream_shm = shm
        wrapper._stream_fps = fps
        wrapper._stream_description = description or (func.__doc__ or "").strip().split("\n")[0]
        wrapper._original_func = func

        return wrapper

    return decorator


def collect_streams(instance: Any) -> dict:
    """Collect all @stream decorated methods from a capability instance.

    Args:
        instance: Capability class instance.

    Returns:
        Dict mapping stream names to their metadata and callables.
    """
    streams = {}
    for attr_name in dir(instance):
        if attr_name.startswith("_"):
            continue
        try:
            attr = getattr(instance, attr_name)
            if callable(attr) and getattr(attr, "_is_device_stream", False):
                stream_name = attr._stream_name
                streams[stream_name] = {
                    "callable": attr,
                    "schema": attr._stream_schema,
                    "shm": attr._stream_shm,
                    "fps": attr._stream_fps,
                    "description": attr._stream_description,
                }
        except Exception:
            pass
    return streams
