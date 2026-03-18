"""Tests for device_connect_server.statectl.cli helper functions."""

import base64
import json
from unittest.mock import MagicMock

from device_connect_server.statectl.cli import (
    _kv_key,
    _decode_value,
    _resolve_key,
    _print_table,
)


# ── _kv_key ───────────────────────────────────────────────────────


class TestKvKey:
    def test_decodes_base64(self):
        raw_key = base64.b64encode(b"/device-connect/state/experiments/EXP-001").decode()
        assert _kv_key({"key": raw_key}) == "/device-connect/state/experiments/EXP-001"

    def test_fallback_on_bad_base64(self):
        assert _kv_key({"key": "not-valid-base64!!!"}) == "not-valid-base64!!!"

    def test_empty_key(self):
        result = _kv_key({"key": ""})
        assert result == ""

    def test_missing_key(self):
        result = _kv_key({})
        assert result == ""


# ── _decode_value ─────────────────────────────────────────────────


class TestDecodeValue:
    def test_json_string(self):
        assert _decode_value('{"status": "done"}') == {"status": "done"}

    def test_bytes_json(self):
        assert _decode_value(b'{"x": 1}') == {"x": 1}

    def test_plain_string(self):
        assert _decode_value("hello") == "hello"

    def test_none(self):
        assert _decode_value(None) is None

    def test_invalid_json_string(self):
        assert _decode_value("not json") == "not json"


# ── _resolve_key ──────────────────────────────────────────────────


class TestResolveKey:
    def test_with_prefix(self):
        result = _resolve_key("experiments/EXP-001", "/device-connect/state/", raw=False)
        assert result == "/device-connect/state/experiments/EXP-001"

    def test_raw_mode(self):
        result = _resolve_key("/custom/key", "/device-connect/state/", raw=True)
        assert result == "/custom/key"


# ── _print_table ──────────────────────────────────────────────────


class TestPrintTable:
    def test_empty(self, capsys):
        _print_table([])
        assert "(empty)" in capsys.readouterr().out

    def test_rows(self, capsys):
        _print_table([("key1", "val1"), ("key2", {"nested": True})])
        out = capsys.readouterr().out
        assert "key1" in out
        assert "val1" in out
        assert "key2" in out

    def test_long_value_truncated(self, capsys):
        long_val = "x" * 200
        _print_table([("k", long_val)])
        out = capsys.readouterr().out
        assert "..." in out
