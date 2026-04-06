"""Unit tests for device_connect_container.serialization module.

Tests cover:
- RawCodec pass-through
- JsonCodec encode/decode
- FlatBuffersCodec binary_frame built-in type
- CodecRegistry lookup and registration
"""

import json
import struct

import pytest

from device_connect_container.serialization.codec import (
    RawCodec,
    JsonCodec,
    CodecRegistry,
    get_codec,
    register_codec,
)
from device_connect_container.serialization.flatbuf import FlatBuffersCodec


# -- RawCodec --


class TestRawCodec:
    def test_name(self):
        assert RawCodec().name == "raw"

    def test_encode_bytes(self):
        codec = RawCodec()
        assert codec.encode(b"hello") == b"hello"

    def test_encode_bytearray(self):
        codec = RawCodec()
        assert codec.encode(bytearray(b"test")) == b"test"

    def test_encode_memoryview(self):
        codec = RawCodec()
        data = b"data"
        assert codec.encode(memoryview(data)) == b"data"

    def test_encode_non_bytes_raises(self):
        codec = RawCodec()
        with pytest.raises(TypeError):
            codec.encode("string")

    def test_decode_returns_memoryview(self):
        codec = RawCodec()
        mv = memoryview(b"hello")
        result = codec.decode(mv)
        assert isinstance(result, memoryview)
        assert bytes(result) == b"hello"


# -- JsonCodec --


class TestJsonCodec:
    def test_name(self):
        assert JsonCodec().name == "json"

    def test_encode_dict(self):
        codec = JsonCodec()
        result = codec.encode({"key": "value"})
        assert json.loads(result) == {"key": "value"}

    def test_decode_to_dict(self):
        codec = JsonCodec()
        data = json.dumps({"x": 1}).encode()
        result = codec.decode(memoryview(data))
        assert result == {"x": 1}


# -- FlatBuffersCodec --


class TestFlatBuffersCodec:
    def test_name(self):
        assert FlatBuffersCodec().name == "flatbuffers"

    def test_binary_frame_encode_decode(self):
        codec = FlatBuffersCodec()
        payload = b"\x00\x01\x02\x03" * 100

        encoded = codec.encode({"_fb_type": "binary_frame", "payload": payload})
        assert isinstance(encoded, bytes)

        # Decode
        decoded = codec.decode(memoryview(encoded))
        assert isinstance(decoded, dict)
        assert bytes(decoded["payload"]) == payload

    def test_binary_frame_empty_payload(self):
        codec = FlatBuffersCodec()
        encoded = codec.encode({"_fb_type": "binary_frame", "payload": b""})
        decoded = codec.decode(memoryview(encoded))
        assert bytes(decoded["payload"]) == b""

    def test_binary_frame_header_size(self):
        codec = FlatBuffersCodec()
        payload = b"test"
        encoded = codec.encode({"_fb_type": "binary_frame", "payload": payload})

        # Header is 4 bytes (uint32 LE payload length)
        length = struct.unpack("<I", encoded[:4])[0]
        assert length == 4
        assert encoded[4:] == b"test"

    def test_register_custom_type(self):
        codec = FlatBuffersCodec()

        def custom_builder(data):
            return b"custom:" + data.get("value", b"")

        def custom_accessor(raw):
            return {"value": raw[7:]}

        codec.register_type("custom", custom_builder, custom_accessor)

        encoded = codec.encode({"_fb_type": "custom", "value": b"hello"})
        assert encoded == b"custom:hello"


# -- CodecRegistry --


class TestCodecRegistry:
    def test_builtin_codecs_registered(self):
        registry = CodecRegistry()
        assert "raw" in registry.available()
        assert "json" in registry.available()

    def test_get_unknown_raises(self):
        registry = CodecRegistry()
        with pytest.raises(KeyError, match="Unknown codec"):
            registry.get("nonexistent")

    def test_global_get_codec(self):
        codec = get_codec("raw")
        assert codec.name == "raw"
