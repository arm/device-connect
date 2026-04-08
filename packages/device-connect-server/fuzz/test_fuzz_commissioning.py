"""Property-based fuzz tests for PIN parsing.

Run:
    pytest fuzz/test_fuzz_commissioning.py -v
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from device_connect_server.security.commissioning import parse_pin, format_pin


@given(pin=st.text(max_size=200))
@settings(max_examples=5000)
def test_parse_pin_never_crashes(pin):
    """parse_pin must handle arbitrary strings without crashing."""
    result = parse_pin(pin)
    assert isinstance(result, str)
    # Should strip dashes and spaces
    assert "-" not in result
    assert " " not in result


@given(pin=st.from_regex(r"[0-9]{8}", fullmatch=True))
@settings(max_examples=2000)
def test_format_then_parse_roundtrip(pin):
    """format_pin -> parse_pin should roundtrip for valid 8-digit PINs."""
    formatted = format_pin(pin)
    parsed = parse_pin(formatted)
    assert parsed == pin
