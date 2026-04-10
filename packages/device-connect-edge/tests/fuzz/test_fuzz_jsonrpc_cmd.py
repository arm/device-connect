"""Property-based fuzz tests for JSON-RPC command parsing.

Uses hypothesis for portable, pytest-integrated fuzzing.
These tests exercise the same code paths as fuzz_jsonrpc_cmd.py (atheris)
but run on any platform without needing libFuzzer.

Run:
    pytest tests/fuzz/test_fuzz_jsonrpc_cmd.py -v
    pytest tests/fuzz/test_fuzz_jsonrpc_cmd.py -v --hypothesis-seed=0  # reproducible
"""

import json

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from device_connect_edge.device import build_rpc_response, build_rpc_error
from device_connect_edge.telemetry.propagation import extract_from_meta


# --- Strategies ---

json_primitives = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**53), max_value=2**53),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=200),
)

json_values = st.recursive(
    json_primitives,
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(st.text(max_size=20), children, max_size=5),
    ),
    max_leaves=20,
)

jsonrpc_like = st.fixed_dictionaries(
    {},
    optional={
        "jsonrpc": st.sampled_from(["2.0", "1.0", "", None, 2]),
        "method": st.one_of(st.text(max_size=100), st.none(), st.integers()),
        "id": st.one_of(st.text(max_size=50), st.integers(), st.none()),
        "params": json_values,
    },
)


# --- Tests ---

@given(data=st.binary(max_size=4096))
@settings(max_examples=5000, suppress_health_check=[HealthCheck.too_slow])
def test_jsonrpc_raw_bytes_never_crashes(data):
    """Raw bytes must never cause an unhandled exception in the parsing path."""
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    if not isinstance(payload, dict):
        return

    payload.get("method")
    if "id" not in payload:
        return

    params_dict = payload.get("params", {})
    if not isinstance(params_dict, dict):
        return

    dc_meta = params_dict.pop("_dc_meta", {})
    if isinstance(dc_meta, dict):
        extract_from_meta(dc_meta)


@given(msg=jsonrpc_like)
@settings(max_examples=5000, suppress_health_check=[HealthCheck.too_slow])
def test_jsonrpc_structured_never_crashes(msg):
    """Structured JSON-RPC-like dicts must never crash the parsing path."""
    method = msg.get("method")
    if "id" not in msg:
        return

    msg_id = msg["id"]
    params_dict = msg.get("params", {})
    if not isinstance(params_dict, dict):
        return

    dc_meta = params_dict.pop("_dc_meta", {})
    if isinstance(dc_meta, dict):
        extract_from_meta(dc_meta)

    if method and isinstance(method, str) and msg_id is not None:
        build_rpc_response(str(msg_id), {"status": "ok"})
        build_rpc_error(str(msg_id), -32601, f"Unknown method: {method}")


@given(meta=st.dictionaries(st.text(max_size=50), st.text(max_size=200), max_size=10))
@settings(max_examples=2000)
def test_extract_from_meta_never_crashes(meta):
    """extract_from_meta must handle arbitrary dict input."""
    extract_from_meta(meta)
