"""Tests for device_connect_sdk.telemetry.file_buffer_exporter module."""

import json
from unittest.mock import MagicMock, patch


from device_connect_sdk.telemetry.file_buffer_exporter import (
    FileBufferSpanExporter,
    _span_to_dict,
)


# ── helpers ───────────────────────────────────────────────────────


def _make_mock_span(name="test-span", trace_id=0x1234, span_id=0x5678):
    """Create a minimal mock span."""
    ctx = MagicMock()
    ctx.trace_id = trace_id
    ctx.span_id = span_id

    span = MagicMock()
    span.name = name
    span.get_span_context.return_value = ctx
    span.parent = None
    span.start_time = 1000
    span.end_time = 2000
    span.kind = None
    span.status = None
    span.attributes = {"key": "value"}
    span.events = []
    span.resource = None
    return span


class _FakeExportResult:
    SUCCESS = 0
    FAILURE = 1


# ── _span_to_dict ────────────────────────────────────────────────


class TestSpanToDict:
    def test_basic_conversion(self):
        span = _make_mock_span()
        d = _span_to_dict(span)
        assert d["name"] == "test-span"
        assert d["start_time"] == 1000
        assert d["end_time"] == 2000
        assert d["attributes"] == {"key": "value"}

    def test_trace_and_span_id_formatted(self):
        span = _make_mock_span(trace_id=0xABCDEF, span_id=0x123)
        d = _span_to_dict(span)
        assert d["trace_id"] == format(0xABCDEF, "032x")
        assert d["span_id"] == format(0x123, "016x")

    def test_no_parent(self):
        span = _make_mock_span()
        d = _span_to_dict(span)
        assert d["parent_span_id"] == ""


# ── FileBufferSpanExporter ────────────────────────────────────────


class TestFileBufferSpanExporter:
    def test_delegate_success(self, tmp_path):
        delegate = MagicMock()
        delegate.export.return_value = _FakeExportResult.SUCCESS

        with patch("device_connect_sdk.telemetry.file_buffer_exporter.SpanExportResult", _FakeExportResult):
            exporter = FileBufferSpanExporter(
                delegate=delegate,
                buffer_dir=str(tmp_path / "buf"),
                drain_interval_s=9999,  # don't auto-drain
            )
            spans = [_make_mock_span()]
            result = exporter.export(spans)
            assert result == _FakeExportResult.SUCCESS
            delegate.export.assert_called_once_with(spans)
            # No files buffered
            buf_files = list((tmp_path / "buf").glob("spans_*.json"))
            assert len(buf_files) == 0
            exporter.shutdown()

    def test_delegate_failure_buffers_to_disk(self, tmp_path):
        delegate = MagicMock()
        delegate.export.return_value = _FakeExportResult.FAILURE

        with patch("device_connect_sdk.telemetry.file_buffer_exporter.SpanExportResult", _FakeExportResult):
            exporter = FileBufferSpanExporter(
                delegate=delegate,
                buffer_dir=str(tmp_path / "buf"),
                drain_interval_s=9999,
            )
            spans = [_make_mock_span("buffered")]
            exporter.export(spans)

            buf_files = list((tmp_path / "buf").glob("spans_*.json"))
            assert len(buf_files) == 1
            data = json.loads(buf_files[0].read_text())
            assert len(data) == 1
            assert data[0]["name"] == "buffered"
            exporter.shutdown()

    def test_delegate_exception_buffers_to_disk(self, tmp_path):
        delegate = MagicMock()
        delegate.export.side_effect = ConnectionError("offline")

        with patch("device_connect_sdk.telemetry.file_buffer_exporter.SpanExportResult", _FakeExportResult):
            exporter = FileBufferSpanExporter(
                delegate=delegate,
                buffer_dir=str(tmp_path / "buf"),
                drain_interval_s=9999,
            )
            exporter.export([_make_mock_span()])
            buf_files = list((tmp_path / "buf").glob("spans_*.json"))
            assert len(buf_files) == 1
            exporter.shutdown()

    def test_shutdown_after_export(self, tmp_path):
        delegate = MagicMock()
        delegate.export.return_value = _FakeExportResult.SUCCESS

        with patch("device_connect_sdk.telemetry.file_buffer_exporter.SpanExportResult", _FakeExportResult):
            exporter = FileBufferSpanExporter(
                delegate=delegate,
                buffer_dir=str(tmp_path / "buf"),
                drain_interval_s=9999,
            )
            exporter.shutdown()
            # After shutdown, export returns SUCCESS immediately
            result = exporter.export([_make_mock_span()])
            assert result == _FakeExportResult.SUCCESS

    def test_disk_limit_eviction(self, tmp_path):
        delegate = MagicMock()
        delegate.export.return_value = _FakeExportResult.FAILURE

        with patch("device_connect_sdk.telemetry.file_buffer_exporter.SpanExportResult", _FakeExportResult):
            exporter = FileBufferSpanExporter(
                delegate=delegate,
                buffer_dir=str(tmp_path / "buf"),
                max_buffer_mb=0,  # 0 bytes = evict everything immediately
                drain_interval_s=9999,
            )
            exporter.export([_make_mock_span("first")])
            exporter.export([_make_mock_span("second")])
            # With 0 MB limit, files should be evicted
            buf_files = list((tmp_path / "buf").glob("spans_*.json"))
            # At most 1 file survives (the latest write, before eviction runs again)
            assert len(buf_files) <= 1
            exporter.shutdown()

    def test_force_flush_delegates(self, tmp_path):
        delegate = MagicMock()
        delegate.force_flush.return_value = True

        with patch("device_connect_sdk.telemetry.file_buffer_exporter.SpanExportResult", _FakeExportResult):
            exporter = FileBufferSpanExporter(
                delegate=delegate,
                buffer_dir=str(tmp_path / "buf"),
                drain_interval_s=9999,
            )
            assert exporter.force_flush(5000) is True
            delegate.force_flush.assert_called_once_with(5000)
            exporter.shutdown()

    def test_buffer_dir_created(self, tmp_path):
        delegate = MagicMock()
        buf = tmp_path / "deep" / "nested" / "buf"
        assert not buf.exists()

        with patch("device_connect_sdk.telemetry.file_buffer_exporter.SpanExportResult", _FakeExportResult):
            exporter = FileBufferSpanExporter(
                delegate=delegate,
                buffer_dir=str(buf),
                drain_interval_s=9999,
            )
            assert buf.exists()
            exporter.shutdown()
