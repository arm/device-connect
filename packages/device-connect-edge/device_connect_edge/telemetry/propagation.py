"""W3C TraceContext propagation through _dc_meta dicts.

Injects/extracts traceparent and tracestate into the _dc_meta
dictionaries that Device Connect passes through NATS messages (JSON-RPC
params and event payloads).

When OTel is not installed, falls back to generating a simple
trace_id field for basic log correlation.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Mapping

try:
    from opentelemetry import context as otel_context
    from opentelemetry.context import Context
    from opentelemetry.propagate import get_global_textmap

    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False
    Context = object  # type: ignore[misc,assignment]


class _DictCarrier(dict):
    """Dict-based carrier for OTel TextMap propagation.

    The W3C TraceContext propagator reads/writes 'traceparent'
    and 'tracestate' keys in this dict.
    """
    pass


def inject_into_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Inject current OTel span context into a _dc_meta dict.

    Adds 'traceparent' and 'tracestate' W3C fields to the dict.
    When OTel is not installed, adds a 'trace_id' field with a
    random UUID for basic log correlation.

    Args:
        meta: The _dc_meta dictionary to inject into.
            Modified in place AND returned.

    Returns:
        The modified meta dict.
    """
    if _OTEL_AVAILABLE:
        carrier = _DictCarrier(meta)
        get_global_textmap().inject(carrier)
        meta.update(carrier)
        # If no active span, inject adds nothing — fall back to trace_id
        if "traceparent" not in meta and "trace_id" not in meta:
            meta["trace_id"] = uuid.uuid4().hex[:16]
    else:
        # Fallback: simple trace_id for log correlation
        if "trace_id" not in meta:
            meta["trace_id"] = uuid.uuid4().hex[:16]

    return meta


def extract_from_meta(meta: Mapping[str, Any]) -> Any:
    """Extract OTel context from a _dc_meta dict.

    Reads 'traceparent' and 'tracestate' W3C fields.
    Falls back to an empty context if fields are missing
    or OTel is not installed.

    Args:
        meta: The _dc_meta dictionary to extract from.

    Returns:
        An OTel Context with the extracted span context,
        or the current context if extraction fails.
    """
    if _OTEL_AVAILABLE:
        carrier = _DictCarrier(meta)
        return get_global_textmap().extract(carrier)

    return None


def inject_into_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Inject OTel context into an event payload dict.

    Similar to inject_into_meta but for event payloads.
    Adds '_traceparent' and '_tracestate' prefixed keys to
    avoid colliding with user-defined payload fields.

    Args:
        payload: Event payload dictionary.

    Returns:
        The modified payload dict.
    """
    if _OTEL_AVAILABLE:
        carrier: Dict[str, Any] = {}
        get_global_textmap().inject(carrier)
        if "traceparent" in carrier:
            payload["_traceparent"] = carrier["traceparent"]
        if "tracestate" in carrier:
            payload["_tracestate"] = carrier["tracestate"]
        # If no active span, inject adds nothing — fall back to _trace_id
        if "_traceparent" not in payload and "_trace_id" not in payload:
            payload["_trace_id"] = uuid.uuid4().hex[:16]
    else:
        if "_trace_id" not in payload:
            payload["_trace_id"] = uuid.uuid4().hex[:16]

    return payload


def extract_from_payload(payload: Mapping[str, Any]) -> Any:
    """Extract OTel context from an event payload.

    Reads '_traceparent' and '_tracestate' from event payloads.

    Args:
        payload: Event payload dictionary.

    Returns:
        An OTel Context, or None if OTel is not installed.
    """
    if _OTEL_AVAILABLE:
        carrier: Dict[str, str] = {}
        if "_traceparent" in payload:
            carrier["traceparent"] = payload["_traceparent"]
        if "_tracestate" in payload:
            carrier["tracestate"] = payload["_tracestate"]
        if carrier:
            return get_global_textmap().extract(carrier)
        return otel_context.get_current()

    return None
