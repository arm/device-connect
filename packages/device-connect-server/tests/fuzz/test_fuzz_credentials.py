# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Property-based fuzz tests for server CredentialsLoader.

Run:
    pytest tests/fuzz/test_fuzz_credentials.py -v
"""

import json

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from device_connect_server.security.credentials import CredentialsLoader


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
        st.dictionaries(st.text(max_size=20), children, max_size=5),
    ),
    max_leaves=15,
)


@given(data=st.dictionaries(st.text(max_size=30), json_values, max_size=10))
@settings(max_examples=5000, suppress_health_check=[HealthCheck.too_slow])
def test_parse_json_format_never_crashes(data):
    """_parse_json_format must not crash on arbitrary dicts."""
    content = json.dumps(data)
    try:
        CredentialsLoader._parse_json_format(content, "<fuzz>")
    except (ValueError, TypeError, KeyError, AttributeError):
        pass


@given(content=st.text(max_size=2000))
@settings(max_examples=5000, suppress_health_check=[HealthCheck.too_slow])
def test_parse_nats_creds_format_never_crashes(content):
    """_parse_nats_creds_format must always return a dict."""
    result = CredentialsLoader._parse_nats_creds_format(content)
    assert isinstance(result, dict)
    for key in ("jwt", "nkey_seed"):
        if key in result:
            assert isinstance(result[key], str)


@given(content=st.binary(max_size=2000))
@settings(max_examples=3000, suppress_health_check=[HealthCheck.too_slow])
def test_parse_nats_creds_format_raw_bytes(content):
    """_parse_nats_creds_format must handle decoded binary."""
    text = content.decode("utf-8", errors="replace")
    result = CredentialsLoader._parse_nats_creds_format(text)
    assert isinstance(result, dict)
