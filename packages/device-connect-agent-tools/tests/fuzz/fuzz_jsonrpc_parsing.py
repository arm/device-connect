"""Atheris fuzz target: JSON-RPC message parsing from connection layer.

Run:
    python tests/fuzz/fuzz_jsonrpc_parsing.py tests/fuzz/corpus/jsonrpc_messages/ -max_total_time=300
"""

import sys
import json

import atheris

with atheris.instrument_imports():
    from device_connect_agent_tools.connection import (
        parse_buffered_payload,
        parse_event_payload,
    )


def TestOneInput(data: bytes) -> None:
    # Always test buffered parsing (never crashes)
    result = parse_buffered_payload(data)
    assert isinstance(result, dict)

    # Test event parsing (may fail on non-JSON)
    try:
        result = parse_event_payload(data)
        assert isinstance(result, dict)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        pass


def main():
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
