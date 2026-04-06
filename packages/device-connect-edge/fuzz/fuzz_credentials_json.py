"""Fuzz target: JSON credentials file loader.

Exercises MessagingConfig._load_credentials_file() which parses JSON
credential files and falls back to NATS .creds format on JSONDecodeError.

Run:
    python fuzz/fuzz_credentials_json.py fuzz/corpus/credentials_json/ -max_total_time=300
"""

import sys
import os
import tempfile

import atheris

with atheris.instrument_imports():
    from device_connect_edge.messaging.config import MessagingConfig


def TestOneInput(data: bytes) -> None:
    """Write fuzzed data to a temp file and load it as a credentials file."""
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        result = MessagingConfig._load_credentials_file(path)
        # Should always return a dict
        assert isinstance(result, dict)
    except (OSError, IOError, UnicodeDecodeError):
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
