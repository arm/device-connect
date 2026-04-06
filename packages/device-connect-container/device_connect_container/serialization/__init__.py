"""Serialization codecs for Device Connect data plane.

Provides FlatBuffers-based zero-copy serialization for SHM data paths
and a codec registry for switching between serialization formats.
"""

from device_connect_container.serialization.codec import Codec, CodecRegistry, RawCodec
from device_connect_container.serialization.flatbuf import FlatBuffersCodec

__all__ = [
    "Codec",
    "CodecRegistry",
    "RawCodec",
    "FlatBuffersCodec",
]
