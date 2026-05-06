# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for dc-portalctl: parser, env handling, output, stream guards."""

from __future__ import annotations

import io
import json

import pytest

from device_connect_server.portalctl import cli as portalctl_cli
from device_connect_server.portalctl import streaming as portalctl_stream


# ── parser ────────────────────────────────────────────────────────


class TestParser:
    def test_top_level_help(self):
        p = portalctl_cli._build_parser()
        # All required subcommands wired
        sub_names = {a.dest for a in p._actions if a.dest}
        assert "cmd" in sub_names

    def test_devices_status_requires_id(self):
        p = portalctl_cli._build_parser()
        with pytest.raises(SystemExit):
            p.parse_args(["devices", "status"])

    def test_provision_accepts_metadata(self):
        p = portalctl_cli._build_parser()
        ns = p.parse_args([
            "devices", "provision", "cam-001",
            "--device-type", "camera",
            "--metadata", "site=lab",
            "--metadata", "rack=3",
        ])
        assert ns.device_name == "cam-001"
        assert ns.device_type == "camera"
        assert ns.metadata == ["site=lab", "rack=3"]


# ── config resolution ─────────────────────────────────────────────


class TestConfigResolution:
    def test_no_token_errors(self, monkeypatch, capsys):
        monkeypatch.delenv("DEVICE_CONNECT_PORTAL_TOKEN", raising=False)
        ns = portalctl_cli._build_parser().parse_args(["auth", "me"])
        ns.token = None
        ns.portal_url = None
        with pytest.raises(SystemExit):
            portalctl_cli._resolve_config(ns)

    def test_env_token_picked_up(self, monkeypatch):
        monkeypatch.setenv("DEVICE_CONNECT_PORTAL_TOKEN", "dcp_env_secret")
        monkeypatch.setenv("DEVICE_CONNECT_PORTAL_URL", "http://h:9000")
        ns = portalctl_cli._build_parser().parse_args(["auth", "me"])
        ns.token = None
        ns.portal_url = None
        url, token = portalctl_cli._resolve_config(ns)
        assert url == "http://h:9000"
        assert token == "dcp_env_secret"


# ── exit code mapping ─────────────────────────────────────────────


class TestExitCodes:
    @pytest.mark.parametrize("status,expected", [
        (200, 0), (201, 0), (204, 0),
        (401, 4), (403, 5), (404, 6),
        (400, 1), (500, 1), (502, 1),
    ])
    def test_status_to_exit(self, status, expected):
        assert portalctl_cli._exit_for_status(status, {}) == expected


# ── output formatters ─────────────────────────────────────────────


class TestOutput:
    def test_json_output_pretty(self, capsys):
        portalctl_cli._emit({"k": 1}, "json")
        out = capsys.readouterr().out
        # Pretty-printed
        assert "\n" in out
        assert json.loads(out) == {"k": 1}

    def test_compact_device_list(self, capsys):
        portalctl_cli._emit(
            [{"device_id": "cam-1", "identity": {"device_type": "camera"},
              "status": {"availability": "available"}}],
            "compact",
        )
        out = capsys.readouterr().out
        assert "cam-1" in out
        assert "camera" in out
        assert "available" in out


# ── stream argument guard ─────────────────────────────────────────


class TestStreamGuard:
    def test_stream_without_bound_errors(self, capsys, monkeypatch):
        monkeypatch.setenv("DEVICE_CONNECT_PORTAL_TOKEN", "dcp_t_s")
        rc = portalctl_cli.main(["devices", "stream", "cam-1", "motion"])
        err = capsys.readouterr().err
        assert rc == 2
        assert "duration" in err and "count" in err and "follow" in err


# ── stream_ndjson termination semantics ──────────────────────────


class _FakeBody:
    def __init__(self, lines: list[bytes]):
        self._lines = lines

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeResp:
    def __init__(self, lines, status=200):
        self.status = status
        self.content = _FakeBody(lines)
        self._text = ""

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, lines, status=200):
        self.lines = lines
        self.status = status

    def get(self, url, headers=None):
        return _FakeResp(list(self.lines), status=self.status)


class TestStreamNdjsonTermination:
    async def test_stops_on_count(self):
        lines = [
            (json.dumps({"event": "motion", "n": 1}) + "\n").encode(),
            (json.dumps({"event": "motion", "n": 2}) + "\n").encode(),
            (json.dumps({"event": "motion", "n": 3}) + "\n").encode(),
        ]
        session = _FakeSession(lines)
        out = io.StringIO()
        rc, events, closed_by = await portalctl_stream.stream_ndjson(
            session, "http://x", {}, duration=None, count=2, follow=False, out=out,
        )
        assert rc == 0
        assert events == 2
        assert closed_by == "count"
        # Should have emitted a _meta trailer locally because the server didn't
        text = out.getvalue()
        assert '"_meta"' in text

    async def test_passes_through_server_meta(self):
        lines = [
            (json.dumps({"event": "motion", "n": 1}) + "\n").encode(),
            (json.dumps({"_meta": {"closed_by": "duration",
                                    "events_received": 1,
                                    "elapsed_s": 5.0}}) + "\n").encode(),
        ]
        out = io.StringIO()
        session = _FakeSession(lines)
        rc, events, closed_by = await portalctl_stream.stream_ndjson(
            session, "http://x", {}, duration=10, count=None, follow=False, out=out,
        )
        assert closed_by == "duration"
        # The first event passed through; trailer printed once (server's)
        assert out.getvalue().count('"_meta"') == 1

    async def test_no_events_yields_exit_code_2(self):
        # Empty body, simulate duration close (no events)
        lines = [
            (json.dumps({"_meta": {"closed_by": "duration",
                                    "events_received": 0,
                                    "elapsed_s": 1.0}}) + "\n").encode(),
        ]
        out = io.StringIO()
        session = _FakeSession(lines)
        rc, events, closed_by = await portalctl_stream.stream_ndjson(
            session, "http://x", {}, duration=1, count=None, follow=False, out=out,
        )
        assert closed_by == "duration"
        assert events == 0
        assert rc == 2

    async def test_http_error_returns_1(self):
        session = _FakeSession([], status=500)
        out = io.StringIO()
        rc, events, closed_by = await portalctl_stream.stream_ndjson(
            session, "http://x", {}, duration=1, count=None, follow=False, out=out,
        )
        assert rc == 1
        assert events == 0
