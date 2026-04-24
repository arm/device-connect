# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Atheris fuzz target: server CredentialsLoader.

Run:
    python tests/fuzz/fuzz_credentials.py fuzz/corpus/credentials_json/ -max_total_time=300
"""

import sys
import json

import atheris

with atheris.instrument_imports():
    from device_connect_server.security.credentials import CredentialsLoader


def TestOneInput(data: bytes) -> None:
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return

    # Try JSON parsing path
    if text.strip().startswith("{"):
        try:
            CredentialsLoader._parse_json_format(text, "<fuzz>")
        except (ValueError, TypeError, KeyError, AttributeError,
                json.JSONDecodeError):
            pass

    # Try NATS creds parsing path
    CredentialsLoader._parse_nats_creds_format(text)


def main():
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
