"""Property-based fuzz tests for JSON credentials file loader.

Run:
    pytest fuzz/test_fuzz_credentials_json.py -v
"""

import json
import os
import tempfile

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from device_connect_edge.messaging.config import MessagingConfig


json_primitives = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**53), max_value=2**53),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=100),
)

json_values = st.recursive(
    json_primitives,
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(st.text(max_size=20), children, max_size=3),
    ),
    max_leaves=10,
)


@given(data=st.dictionaries(st.text(max_size=30), json_values, max_size=10))
@settings(max_examples=3000, suppress_health_check=[HealthCheck.too_slow])
def test_load_credentials_json_never_crashes(data):
    """_load_credentials_file with valid JSON must always return a dict."""
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        result = MessagingConfig._load_credentials_file(path)
        assert isinstance(result, dict)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@given(data=st.binary(max_size=2000))
@settings(max_examples=3000, suppress_health_check=[HealthCheck.too_slow])
def test_load_credentials_raw_bytes_never_crashes(data):
    """_load_credentials_file with arbitrary bytes must not crash."""
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        result = MessagingConfig._load_credentials_file(path)
        assert isinstance(result, dict)
    except UnicodeDecodeError:
        pass  # Acceptable when file contains non-UTF-8 bytes
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@given(
    jwt=st.text(max_size=200),
    nkey_seed=st.text(max_size=200),
)
@settings(max_examples=2000)
def test_load_credentials_nested_nats_format(jwt, nkey_seed):
    """Nested NATS format should extract jwt and nkey_seed correctly."""
    data = {"nats": {"jwt": jwt, "nkey_seed": nkey_seed}}
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        result = MessagingConfig._load_credentials_file(path)
        assert result["jwt"] == jwt
        assert result["nkey_seed"] == nkey_seed
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
