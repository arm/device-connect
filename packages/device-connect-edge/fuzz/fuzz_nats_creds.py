"""Fuzz target: NATS .creds file parser.

Exercises MessagingConfig._parse_nats_creds_file() which uses manual
string parsing with .find() and slicing to extract JWT and NKey seed
from NATS credential files.

Run:
    python fuzz/fuzz_nats_creds.py fuzz/corpus/nats_creds/ -max_total_time=300
"""

import sys
import os
import tempfile

import atheris

with atheris.instrument_imports():
    from device_connect_edge.messaging.config import MessagingConfig


def TestOneInput(data: bytes) -> None:
    """Write fuzzed data to a temp file and parse it as a .creds file."""
    try:
        content = data.decode("utf-8", errors="replace")
    except Exception:
        return

    # _parse_nats_creds_file reads from a file path, so write to temp file
    fd, path = tempfile.mkstemp(suffix=".creds")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        result = MessagingConfig._parse_nats_creds_file(path)
        # Verify the result is always a dict
        assert isinstance(result, dict)
    except (OSError, IOError):
        pass
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def main():
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
