"""Singleton tracer and span helpers for Device Connect.

Aligned with strands.telemetry.tracer — singleton get_tracer() pattern
with imperative span helpers (not decorator-based).
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

try:
    from opentelemetry import trace as trace_api
    from opentelemetry.trace import (
        Span,
        SpanKind,
        StatusCode,
    )

    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False

    # No-op types for when OTel is not installed
    class Span:  # type: ignore[no-redef]
        pass

    class SpanKind:  # type: ignore[no-redef]
        SERVER = None
        CLIENT = None
        PRODUCER = None
        CONSUMER = None
        INTERNAL = None

    class StatusCode:  # type: ignore[no-redef]
        OK = None
        ERROR = None
        UNSET = None


_TRACER_NAME = "device_connect"
_TRACER_VERSION = "0.2.2"


def get_tracer() -> Any:
    """Get the singleton Device Connect tracer.

    Returns the OTel tracer if available, or a no-op tracer.
    Like strands.telemetry.get_tracer(), this always returns
    a usable tracer — never None.
    """
    if not _OTEL_AVAILABLE:
        return trace_api.get_tracer(_TRACER_NAME) if False else _NoOpTracer()

    return trace_api.get_tracer(_TRACER_NAME, _TRACER_VERSION)


def get_current_trace_id() -> str:
    """Get the current trace ID as a hex string.

    Returns the W3C trace_id from the active OTel span context,
    or generates a random UUID hex if no span is active or OTel
    is not installed.
    """
    if _OTEL_AVAILABLE:
        span = trace_api.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.is_valid:
            return format(ctx.trace_id, "032x")

    return uuid.uuid4().hex


def get_current_span_id() -> str:
    """Get the current span ID as a hex string."""
    if _OTEL_AVAILABLE:
        span = trace_api.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.is_valid:
            return format(ctx.span_id, "016x")

    return "0" * 16


class _NoOpSpan:
    """Minimal no-op span for when OTel is not installed."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, status: Any, description: Optional[str] = None) -> None:
        pass

    def record_exception(self, exception: BaseException, **kwargs: Any) -> None:
        pass

    def add_event(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> None:
        pass

    def end(self) -> None:
        pass

    def __enter__(self) -> "_NoOpSpan":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class _NoOpTracer:
    """Minimal no-op tracer for when OTel is not installed."""

    def start_span(self, name: str, **kwargs: Any) -> _NoOpSpan:
        return _NoOpSpan()

    def start_as_current_span(
        self, name: str, **kwargs: Any
    ) -> _NoOpSpan:
        return _NoOpSpan()
