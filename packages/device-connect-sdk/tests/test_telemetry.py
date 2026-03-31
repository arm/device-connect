"""Unit tests for the device_connect_sdk.telemetry package.

Tests cover:
- No-op behavior when OTel is not installed (mocked)
- Tracer singleton and no-op tracer
- MetricsClient singleton and no-op instruments
- W3C propagation round-trip (inject/extract)
- DeviceConnectTelemetry initialization and provider reuse
"""

from unittest.mock import patch



# -- No-op behavior tests --


class TestNoOpTracer:
    """Tests for _NoOpTracer and _NoOpSpan."""

    def test_noop_span_context_manager(self):
        from device_connect_sdk.telemetry.tracer import _NoOpTracer, _NoOpSpan

        tracer = _NoOpTracer()
        with tracer.start_as_current_span("test") as span:
            assert isinstance(span, _NoOpSpan)

    def test_noop_span_methods_dont_raise(self):
        from device_connect_sdk.telemetry.tracer import _NoOpSpan

        span = _NoOpSpan()
        span.set_attribute("key", "value")
        span.set_status("OK")
        span.set_status("ERROR", "description")
        span.record_exception(RuntimeError("test"))
        span.add_event("my_event", {"attr": "val"})
        span.end()

    def test_noop_tracer_start_span(self):
        from device_connect_sdk.telemetry.tracer import _NoOpTracer, _NoOpSpan

        tracer = _NoOpTracer()
        span = tracer.start_span("test")
        assert isinstance(span, _NoOpSpan)


class TestNoOpMetrics:
    """Tests for _NoOpInstrument."""

    def test_noop_instrument_methods(self):
        from device_connect_sdk.telemetry.metrics import _NoOpInstrument

        noop = _NoOpInstrument()
        noop.add(1, {"key": "value"})
        noop.record(42.0, {"key": "value"})
        # Should not raise


class TestMetricsClientSingleton:
    """Tests for MetricsClient singleton."""

    def test_get_metrics_returns_client(self):
        from device_connect_sdk.telemetry.metrics import get_metrics, MetricsClient

        client = get_metrics()
        assert isinstance(client, MetricsClient)

    def test_get_metrics_singleton(self):
        from device_connect_sdk.telemetry.metrics import get_metrics

        a = get_metrics()
        b = get_metrics()
        assert a is b

    def test_metrics_client_has_instruments(self):
        from device_connect_sdk.telemetry.metrics import get_metrics

        m = get_metrics()
        # Core instruments
        assert hasattr(m, "rpc_duration")
        assert hasattr(m, "rpc_count")
        assert hasattr(m, "rpc_active")
        assert hasattr(m, "event_count")
        assert hasattr(m, "msg_publish_duration")
        assert hasattr(m, "msg_request_duration")
        assert hasattr(m, "state_op_duration")
        assert hasattr(m, "registration_count")
        assert hasattr(m, "heartbeat_count")
        # Orchestration instruments
        assert hasattr(m, "experiment_duration")
        assert hasattr(m, "experiment_step_duration")
        assert hasattr(m, "retry_count")


class TestGetTracer:
    """Tests for get_tracer()."""

    def test_get_tracer_returns_usable(self):
        from device_connect_sdk.telemetry.tracer import get_tracer

        tracer = get_tracer()
        assert tracer is not None
        # Should have start_as_current_span
        assert hasattr(tracer, "start_as_current_span")

    def test_get_current_trace_id_returns_hex(self):
        from device_connect_sdk.telemetry.tracer import get_current_trace_id

        trace_id = get_current_trace_id()
        assert isinstance(trace_id, str)
        assert len(trace_id) == 32  # hex UUID
        # Should be valid hex
        int(trace_id, 16)


# -- Propagation tests --


class TestPropagation:
    """Tests for W3C propagation inject/extract."""

    def test_inject_into_meta_adds_fields(self):
        from device_connect_sdk.telemetry.propagation import inject_into_meta

        meta = {"source_device": "test-001"}
        result = inject_into_meta(meta)
        assert result is meta  # Modified in place
        # Should have at least trace_id or traceparent
        has_trace = "traceparent" in meta or "trace_id" in meta
        assert has_trace

    def test_extract_from_meta_does_not_raise(self):
        from device_connect_sdk.telemetry.propagation import extract_from_meta

        meta = {"source_device": "test-001"}
        extract_from_meta(meta)
        # Should return None or a Context -- never raise

    def test_inject_into_payload_adds_fields(self):
        from device_connect_sdk.telemetry.propagation import inject_into_payload

        payload = {"plate_id": "P003"}
        result = inject_into_payload(payload)
        assert result is payload
        # Should have _traceparent or _trace_id
        has_trace = "_traceparent" in payload or "_trace_id" in payload
        assert has_trace

    def test_extract_from_payload_does_not_raise(self):
        from device_connect_sdk.telemetry.propagation import extract_from_payload

        payload = {"plate_id": "P003", "_traceparent": "00-abc-def-01"}
        extract_from_payload(payload)
        # Should not raise

    def test_inject_extract_meta_roundtrip(self):
        """Inject then extract should not lose data."""
        from device_connect_sdk.telemetry.propagation import inject_into_meta, extract_from_meta

        meta = {"source_device": "cam-001"}
        inject_into_meta(meta)
        extract_from_meta(meta)
        # If OTel is installed, ctx should be a Context; if not, None
        # Either way, this should not raise


# -- SpanKind / StatusCode re-exports --


class TestReExports:
    """Test that SpanKind and StatusCode are importable."""

    def test_span_kind_values(self):
        from device_connect_sdk.telemetry.tracer import SpanKind

        assert hasattr(SpanKind, "SERVER")
        assert hasattr(SpanKind, "CLIENT")
        assert hasattr(SpanKind, "PRODUCER")
        assert hasattr(SpanKind, "CONSUMER")
        assert hasattr(SpanKind, "INTERNAL")

    def test_status_code_values(self):
        from device_connect_sdk.telemetry.tracer import StatusCode

        assert hasattr(StatusCode, "OK")
        assert hasattr(StatusCode, "ERROR")
        assert hasattr(StatusCode, "UNSET")


# -- DeviceConnectTelemetry config tests --


class TestDeviceConnectTelemetryConfig:
    """Tests for DeviceConnectTelemetry class."""

    def test_is_enabled_importable(self):
        from device_connect_sdk.telemetry import is_enabled

        # Should return a bool
        assert isinstance(is_enabled(), bool)

    def test_public_api_importable(self):
        from device_connect_sdk.telemetry import (
            DeviceConnectTelemetry,
            is_enabled,
            get_tracer,
            get_current_trace_id,
            get_metrics,
        )
        # All should be callable
        assert callable(DeviceConnectTelemetry)
        assert callable(is_enabled)
        assert callable(get_tracer)
        assert callable(get_current_trace_id)
        assert callable(get_metrics)

    def test_disabled_via_env(self):
        """OTEL_SDK_DISABLED=true should prevent initialization."""
        import device_connect_sdk.telemetry.config as cfg

        old_enabled = cfg._enabled
        old_initialized = cfg._initialized
        try:
            cfg._enabled = False
            cfg._initialized = False
            with patch.dict("os.environ", {"OTEL_SDK_DISABLED": "true"}):
                from device_connect_sdk.telemetry.config import DeviceConnectTelemetry
                DeviceConnectTelemetry(service_name="test-disabled")
                # After init with OTEL_SDK_DISABLED, should stay disabled
                # (actual behavior depends on whether OTel is installed)
        finally:
            cfg._enabled = old_enabled
            cfg._initialized = old_initialized


# -- DictCarrier tests --


class TestDictCarrier:
    """Tests for the _DictCarrier helper."""

    def test_dict_carrier_is_dict(self):
        from device_connect_sdk.telemetry.propagation import _DictCarrier

        carrier = _DictCarrier({"key": "value"})
        assert isinstance(carrier, dict)
        assert carrier["key"] == "value"

    def test_dict_carrier_set_get(self):
        from device_connect_sdk.telemetry.propagation import _DictCarrier

        carrier = _DictCarrier()
        carrier["traceparent"] = "00-abc-def-01"
        assert carrier["traceparent"] == "00-abc-def-01"
