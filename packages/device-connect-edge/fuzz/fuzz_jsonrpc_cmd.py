"""Fuzz target: JSON-RPC command message parsing.

Exercises the same parsing path as DeviceRuntime._cmd_subscription().on_msg(),
which receives raw bytes from the messaging broker and extracts method, id,
params, and _dc_meta fields.

Run:
    python fuzz/fuzz_jsonrpc_cmd.py fuzz/corpus/jsonrpc_cmd/ -max_total_time=300
"""

import sys
import json

import atheris

with atheris.instrument_imports():
    from device_connect_edge.telemetry.propagation import extract_from_meta


def TestOneInput(data: bytes) -> None:
    """Simulate JSON-RPC command parsing from device.py:1037-1102."""
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    if not isinstance(payload, dict):
        return

    # Mirror the parsing in _cmd_subscription
    method = payload.get("method")

    if "id" not in payload:
        return

    msg_id = payload["id"]
    params_dict = payload.get("params", {})

    if not isinstance(params_dict, dict):
        return

    # Extract _dc_meta (trace metadata)
    dc_meta = params_dict.pop("_dc_meta", {})
    if isinstance(dc_meta, dict):
        source_device = dc_meta.get("source_device")
        # Exercise OTel context extraction
        parent_ctx = extract_from_meta(dc_meta)

    # Exercise response building
    from device_connect_edge.device import build_rpc_response, build_rpc_error

    if method and isinstance(method, str):
        build_rpc_response(str(msg_id), {"status": "ok"})
        build_rpc_error(str(msg_id), -32601, f"Unknown method: {method}")


def main():
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
