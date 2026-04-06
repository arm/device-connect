"""Hypothesis configuration for fuzz tests.

Profiles: 'default' for local dev, 'ci' for thorough CI runs.

Select profile via env var:
    HYPOTHESIS_PROFILE=ci pytest fuzz/test_fuzz_*.py
"""

import os

from hypothesis import settings, HealthCheck

# Default profile: fast iteration during local development
settings.register_profile(
    "default",
    max_examples=5000,
    suppress_health_check=[HealthCheck.too_slow],
)

# CI profile: more examples for thorough coverage
settings.register_profile(
    "ci",
    max_examples=20000,
    suppress_health_check=[HealthCheck.too_slow],
)

settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))
