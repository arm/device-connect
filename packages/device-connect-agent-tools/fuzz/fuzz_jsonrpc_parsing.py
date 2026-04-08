"""Atheris fuzz target: JSON-RPC message parsing from connection layer.

Run:
    python fuzz/fuzz_jsonrpc_parsing.py fuzz/corpus/jsonrpc_messages/ -max_total_time=300
"""

import sys
import json

import atheris


def parse_buffered_message(data: bytes) -> dict:
    """Simulate connection.py:399-403."""
    try:
        payload = json.loads(data.decode())
        if not isinstance(payload, dict):
            payload = {"raw": str(payload)[:500]}
    except Exception:
        payload = {"raw": data.decode("utf-8", errors="replace")[:500]}
    return payload


def parse_event_message(data: bytes) -> dict:
    """Simulate connection.py:499-505."""
    payload = json.loads(data.decode())
    if not isinstance(payload, dict):
        raise ValueError("Expected JSON object")
    method = payload.get("method", "")
    dev_id = payload.get("params", {}).get("device_id", "unknown")
    params = payload.get("params", {})
    return {"device_id": dev_id, "event_name": method, "params": params}


def TestOneInput(data: bytes) -> None:
    # Always test buffered parsing (never crashes)
    result = parse_buffered_message(data)
    assert isinstance(result, dict)

    # Test event parsing (may fail on non-JSON)
    try:
        result = parse_event_message(data)
        assert isinstance(result, dict)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        pass


def main():
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
