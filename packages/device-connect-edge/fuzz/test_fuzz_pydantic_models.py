"""Property-based fuzz tests for Pydantic model validation.

Run:
    pytest fuzz/test_fuzz_pydantic_models.py -v
"""

import json

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st
from pydantic import ValidationError

from device_connect_edge.types import (
    DeviceCapabilities,
    DeviceIdentity,
    DeviceStatus,
    EventDef,
    FunctionDef,
)

MODELS = [FunctionDef, EventDef, DeviceCapabilities, DeviceIdentity, DeviceStatus]

# Strategy: arbitrary JSON-like dicts
json_primitives = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**53), max_value=2**53),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=100),
)

json_values = st.recursive(
    json_primitives,
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(st.text(max_size=20), children, max_size=5),
    ),
    max_leaves=15,
)

arbitrary_dict = st.dictionaries(st.text(max_size=30), json_values, max_size=10)


@given(data=arbitrary_dict, model_idx=st.integers(min_value=0, max_value=len(MODELS) - 1))
@settings(max_examples=5000, suppress_health_check=[HealthCheck.too_slow])
def test_pydantic_models_handle_arbitrary_dicts(data, model_idx):
    """Pydantic models must either validate or raise ValidationError, never crash."""
    model = MODELS[model_idx]
    try:
        model.model_validate(data)
    except (ValidationError, TypeError, ValueError):
        pass  # Expected rejections


@given(raw=st.binary(max_size=4096))
@settings(max_examples=3000, suppress_health_check=[HealthCheck.too_slow])
def test_pydantic_models_handle_raw_bytes(raw):
    """Parsing arbitrary bytes as JSON then validating must not crash."""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    if not isinstance(parsed, dict):
        return

    for model in MODELS:
        try:
            model.model_validate(parsed)
        except (ValidationError, TypeError, ValueError):
            pass


# Targeted strategies for models with constraints
@given(
    busy_score=st.one_of(
        st.floats(allow_nan=True, allow_infinity=True),
        st.integers(),
        st.text(max_size=20),
        st.none(),
    ),
    battery=st.one_of(
        st.integers(min_value=-1000, max_value=1000),
        st.floats(),
        st.text(max_size=20),
        st.none(),
    ),
)
@settings(max_examples=2000)
def test_device_status_constrained_fields(busy_score, battery):
    """DeviceStatus with constrained fields (busy_score 0-1, battery 0-100)."""
    try:
        DeviceStatus.model_validate({
            "busy_score": busy_score,
            "battery": battery,
        })
    except (ValidationError, TypeError, ValueError):
        pass
