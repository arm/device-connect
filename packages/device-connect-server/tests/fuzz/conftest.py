"""Hypothesis configuration for server fuzz tests."""

import os

from hypothesis import settings, HealthCheck

settings.register_profile(
    "default",
    max_examples=5000,
    suppress_health_check=[HealthCheck.too_slow],
)

settings.register_profile(
    "ci",
    max_examples=20000,
    suppress_health_check=[HealthCheck.too_slow],
)

settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))
