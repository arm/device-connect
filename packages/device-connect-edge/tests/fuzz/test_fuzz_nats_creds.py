# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Property-based fuzz tests for NATS .creds file parser.

Run:
    pytest tests/fuzz/test_fuzz_nats_creds.py -v
"""

import os
import tempfile

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from device_connect_edge.messaging.config import MessagingConfig


# Strategies that include the real markers to exercise extraction logic
nats_markers = st.sampled_from([
    "-----BEGIN NATS USER JWT-----",
    "------END NATS USER JWT------",
    "-----BEGIN USER NKEY SEED-----",
    "------END USER NKEY SEED------",
    "",
])

creds_content = st.one_of(
    # Totally random text
    st.text(max_size=2000),
    # Random binary decoded lossily
    st.binary(max_size=2000).map(lambda b: b.decode("utf-8", errors="replace")),
    # Structured with real markers mixed with random content
    st.tuples(
        nats_markers, st.text(max_size=200),
        nats_markers, st.text(max_size=200),
        nats_markers, st.text(max_size=200),
        nats_markers,
    ).map(lambda parts: "\n".join(parts)),
)


@given(content=creds_content)
@settings(max_examples=3000, suppress_health_check=[HealthCheck.too_slow])
def test_parse_nats_creds_never_crashes(content):
    """_parse_nats_creds_file must always return a dict, never crash."""
    fd, path = tempfile.mkstemp(suffix=".creds")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        result = MessagingConfig._parse_nats_creds_file(path)
        assert isinstance(result, dict)
        # If keys present, they should be strings
        for key in ("jwt", "nkey_seed"):
            if key in result:
                assert isinstance(result[key], str)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@given(content=st.text(max_size=500))
@settings(max_examples=2000)
def test_parse_nats_creds_roundtrip(content):
    """If markers are present, extracted values should be substrings of input."""
    jwt_begin = "-----BEGIN NATS USER JWT-----"
    jwt_end = "------END NATS USER JWT------"
    nkey_begin = "-----BEGIN USER NKEY SEED-----"
    nkey_end = "------END USER NKEY SEED------"

    full = f"{jwt_begin}\n{content}\n{jwt_end}\n{nkey_begin}\n{content}\n{nkey_end}\n"

    fd, path = tempfile.mkstemp(suffix=".creds")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(full)
        result = MessagingConfig._parse_nats_creds_file(path)
        assert isinstance(result, dict)
        # Markers are present, so jwt and nkey_seed should be extracted
        assert "jwt" in result
        assert "nkey_seed" in result
        assert isinstance(result["jwt"], str)
        assert isinstance(result["nkey_seed"], str)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
