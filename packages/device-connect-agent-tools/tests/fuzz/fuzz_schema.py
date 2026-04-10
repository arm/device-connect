"""Atheris fuzz target: MCP tool name parsing.

Run:
    python tests/fuzz/fuzz_schema.py fuzz/corpus/tool_names/ -max_total_time=300
"""

import sys

import atheris

with atheris.instrument_imports():
    from device_connect_agent_tools.mcp.schema import parse_tool_name


def TestOneInput(data: bytes) -> None:
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return

    try:
        result = parse_tool_name(text)
        assert isinstance(result, tuple)
        assert len(result) == 2
    except ValueError:
        pass


def main():
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
