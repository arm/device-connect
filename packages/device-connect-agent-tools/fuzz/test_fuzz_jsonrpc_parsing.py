"""Property-based fuzz tests for JSON-RPC message parsing in connection layer.

Run:
    pytest fuzz/test_fuzz_jsonrpc_parsing.py -v
"""

import json

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st


def parse_buffered_message(data: bytes) -> dict:
    """Simulate the buffered subscription parsing from connection.py:399-403."""
    try:
        payload = json.loads(data.decode())
        if not isinstance(payload, dict):
            payload = {"raw": str(payload)[:500]}
    except Exception:
        payload = {"raw": data.decode("utf-8", errors="replace")[:500]}
    return payload


def parse_event_message(data: bytes) -> dict:
    """Simulate the event parsing from connection.py:499-505."""
    payload = json.loads(data.decode())
    if not isinstance(payload, dict):
        raise ValueError("Expected JSON object")
    method = payload.get("method", "")
    dev_id = payload.get("params", {}).get("device_id", "unknown")
    params = payload.get("params", {})
    return {"device_id": dev_id, "event_name": method, "params": params}


@given(data=st.binary(max_size=4096))
@settings(max_examples=5000, suppress_health_check=[HealthCheck.too_slow])
def test_buffered_message_parsing_never_crashes(data):
    """Buffered message parsing must always return a dict."""
    result = parse_buffered_message(data)
    assert isinstance(result, dict)


@given(data=st.binary(max_size=4096))
@settings(max_examples=5000, suppress_health_check=[HealthCheck.too_slow])
def test_event_parsing_handles_arbitrary_bytes(data):
    """Event parsing should handle or reject arbitrary bytes gracefully."""
    try:
        result = parse_event_message(data)
        assert isinstance(result, dict)
        assert "device_id" in result
        assert "event_name" in result
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        pass


json_primitives = st.one_of(
    st.none(), st.booleans(), st.integers(), st.text(max_size=100),
    st.floats(allow_nan=False, allow_infinity=False),
)

json_values = st.recursive(
    json_primitives,
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(st.text(max_size=20), children, max_size=5),
    ),
    max_leaves=10,
)


@given(payload=st.dictionaries(st.text(max_size=30), json_values, max_size=8))
@settings(max_examples=5000, suppress_health_check=[HealthCheck.too_slow])
def test_event_parsing_structured_input(payload):
    """Event parsing with structured JSON dicts must not crash."""
    data = json.dumps(payload).encode()
    result = parse_event_message(data)
    assert isinstance(result, dict)
