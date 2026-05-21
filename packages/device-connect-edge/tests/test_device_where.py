# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for edge-side broadcast ``where`` runtime behavior."""

import time
from unittest.mock import Mock

from device_connect_edge import DeviceRuntime


class _SlowPredicate:
    def evaluate(self, context):
        time.sleep(0.2)
        return True


class _RaisingPredicate:
    def evaluate(self, context):
        raise RuntimeError("bad predicate")


def test_where_eval_timeout_fails_closed_and_warns():
    runtime = DeviceRuntime(device_id="where-timeout-test")
    runtime._logger = Mock()

    result = runtime._evaluate_where_with_timeout(
        _SlowPredicate(), {}, "corr-timeout", timeout_s=0.01,
    )

    assert result is False
    runtime._logger.warning.assert_called_once()
    assert "where predicate timed out" in (
        runtime._logger.warning.call_args.args[0]
    )


def test_where_eval_error_propagates_to_caller():
    runtime = DeviceRuntime(device_id="where-error-test")

    try:
        runtime._evaluate_where_with_timeout(
            _RaisingPredicate(), {}, "corr-error", timeout_s=0.1,
        )
    except RuntimeError as exc:
        assert "bad predicate" in str(exc)
    else:
        raise AssertionError("expected predicate exception to propagate")


def test_missing_predicate_extra_warns_at_startup(monkeypatch):
    import device_connect_edge.predicate as predicate_mod

    def _missing_extra(expression):
        raise predicate_mod.PredicateCompileError("cel-python missing")

    runtime = DeviceRuntime(device_id="where-missing-extra-test")
    runtime._logger = Mock()
    monkeypatch.setattr(predicate_mod, "compile_where", _missing_extra)

    runtime._warn_if_predicate_extra_missing()

    runtime._logger.warning.assert_called_once()
    assert "Edge-side where predicates are unavailable" in (
        runtime._logger.warning.call_args.args[0]
    )
