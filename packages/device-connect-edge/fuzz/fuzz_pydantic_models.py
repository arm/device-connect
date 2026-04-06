"""Fuzz target: Pydantic model validation.

Exercises all core Pydantic models (DeviceIdentity, DeviceStatus,
DeviceCapabilities, FunctionDef, EventDef) with arbitrary JSON input.
Looks for unexpected exceptions during model_validate().

Run:
    python fuzz/fuzz_pydantic_models.py fuzz/corpus/pydantic_models/ -max_total_time=300
"""

import sys
import json

import atheris

with atheris.instrument_imports():
    from pydantic import ValidationError
    from device_connect_edge.types import (
        DeviceCapabilities,
        DeviceIdentity,
        DeviceStatus,
        EventDef,
        FunctionDef,
    )

MODELS = [FunctionDef, EventDef, DeviceCapabilities, DeviceIdentity, DeviceStatus]


def TestOneInput(data: bytes) -> None:
    """Feed fuzzed JSON into each Pydantic model."""
    if len(data) < 2:
        return

    # Use first byte to select model, rest as JSON input
    model_idx = data[0] % len(MODELS)
    remaining = data[1:]

    try:
        parsed = json.loads(remaining)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    if not isinstance(parsed, dict):
        return

    try:
        MODELS[model_idx].model_validate(parsed)
    except (ValidationError, TypeError, ValueError):
        pass  # Expected — Pydantic rejecting bad input


def main():
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
