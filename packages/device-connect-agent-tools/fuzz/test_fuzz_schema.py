"""Property-based fuzz tests for MCP tool name parsing.

Run:
    pytest fuzz/test_fuzz_schema.py -v
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from device_connect_agent_tools.mcp.schema import parse_tool_name


@given(name=st.text(max_size=500))
@settings(max_examples=5000)
def test_parse_tool_name_never_crashes(name):
    """parse_tool_name must either return a tuple or raise ValueError."""
    try:
        result = parse_tool_name(name)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], str)
        assert isinstance(result[1], str)
    except ValueError:
        pass


@given(
    device_id=st.text(min_size=1, max_size=100),
    function_name=st.text(min_size=1, max_size=100),
)
@settings(max_examples=3000)
def test_parse_tool_name_roundtrip(device_id, function_name):
    """Valid device_id::function_name should always parse correctly."""
    # Skip if device_id contains : (would form ambiguous :: boundaries)
    if ":" in device_id:
        return
    tool_name = f"{device_id}::{function_name}"
    dev, func = parse_tool_name(tool_name)
    assert dev == device_id
    assert func == function_name
