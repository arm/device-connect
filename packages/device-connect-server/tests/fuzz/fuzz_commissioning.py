# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Atheris fuzz target: PIN parsing.

Run:
    python tests/fuzz/fuzz_commissioning.py fuzz/corpus/commissioning/ -max_total_time=300
"""

import sys

import atheris

with atheris.instrument_imports():
    from device_connect_server.security.commissioning import parse_pin


def TestOneInput(data: bytes) -> None:
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return

    result = parse_pin(text)
    assert isinstance(result, str)


def main():
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
