"""Codec registry for data plane serialization.

Supports pluggable serialization formats:
- RawCodec: Pass-through (no serialization, for pre-encoded data)
- FlatBuffersCodec: Zero-copy FlatBuffers (preferred for SHM)
- JSON: Standard JSON encoding (fallback for control plane compatibility)

The control plane (RPC, registration, heartbeats) always uses JSON-RPC 2.0.
The data plane (camera frames, sensor streams) uses the codec specified
in the @stream decorator or ShmChannel configuration.
"""

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Type

logger = logging.getLogger(__name__)


class Codec(ABC):
    """Abstract serialization codec for data plane messages."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Codec identifier (e.g., 'flatbuffers', 'json', 'raw')."""
        ...

    @abstractmethod
    def encode(self, data: Any) -> bytes:
        """Serialize data to bytes.

        Args:
            data: Python object to serialize.

        Returns:
            Serialized bytes.
        """
        ...

    @abstractmethod
    def decode(self, raw: memoryview) -> Any:
        """Deserialize bytes to Python object.

        For zero-copy codecs (FlatBuffers), this returns an accessor
        that reads directly from the buffer without copying.

        Args:
            raw: Raw bytes (memoryview for zero-copy).

        Returns:
            Deserialized data or zero-copy accessor.
        """
        ...


class RawCodec(Codec):
    """Pass-through codec — no serialization.

    Data must already be bytes. Useful for pre-encoded payloads
    (e.g., JPEG frames, compressed point clouds).
    """

    @property
    def name(self) -> str:
        return "raw"

    def encode(self, data: Any) -> bytes:
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        if isinstance(data, memoryview):
            return bytes(data)
        raise TypeError(f"RawCodec expects bytes, got {type(data)}")

    def decode(self, raw: memoryview) -> memoryview:
        return raw


class JsonCodec(Codec):
    """JSON codec — for compatibility with existing control plane messages.

    Not zero-copy: requires full deserialization. Use only for small
    payloads or when FlatBuffers schemas aren't defined yet.
    """

    @property
    def name(self) -> str:
        return "json"

    def encode(self, data: Any) -> bytes:
        return json.dumps(data).encode()

    def decode(self, raw: memoryview) -> Any:
        return json.loads(bytes(raw).decode())


class CodecRegistry:
    """Registry of available serialization codecs.

    Provides lookup by name for @stream decorator and ShmChannel.
    """

    def __init__(self) -> None:
        self._codecs: Dict[str, Codec] = {}
        # Register built-in codecs
        self.register(RawCodec())
        self.register(JsonCodec())

    def register(self, codec: Codec) -> None:
        """Register a codec instance."""
        self._codecs[codec.name] = codec

    def get(self, name: str) -> Codec:
        """Get a codec by name.

        Args:
            name: Codec name.

        Returns:
            Codec instance.

        Raises:
            KeyError: If codec not found.
        """
        if name not in self._codecs:
            raise KeyError(
                f"Unknown codec: {name}. Available: {list(self._codecs.keys())}"
            )
        return self._codecs[name]

    def available(self) -> list:
        """List available codec names."""
        return list(self._codecs.keys())


# Global codec registry
_registry = CodecRegistry()


def get_codec(name: str) -> Codec:
    """Get a codec from the global registry."""
    return _registry.get(name)


def register_codec(codec: Codec) -> None:
    """Register a codec in the global registry."""
    _registry.register(codec)
