# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for edge-side broadcast ``where`` runtime behavior."""

import asyncio
import threading
import time
from unittest.mock import Mock

import pytest

from device_connect_edge import DeviceRuntime


class _SlowPredicate:
    def evaluate(self, context):
        time.sleep(0.2)
        return True


class _RaisingPredicate:
    def evaluate(self, context):
        raise RuntimeError("bad predicate")


@pytest.mark.asyncio
async def test_where_eval_timeout_fails_closed_and_warns():
    runtime = DeviceRuntime(device_id="where-timeout-test")
    runtime._logger = Mock()

    result = await runtime._evaluate_where_with_timeout(
        _SlowPredicate(), {}, "corr-timeout", timeout_s=0.01,
    )

    assert result is False
    runtime._logger.warning.assert_called_once()
    assert "where predicate timed out" in (
        runtime._logger.warning.call_args.args[0]
    )
    runtime._shutdown_where_eval_executor()


@pytest.mark.asyncio
async def test_where_eval_error_propagates_to_caller():
    runtime = DeviceRuntime(device_id="where-error-test")

    try:
        await runtime._evaluate_where_with_timeout(
            _RaisingPredicate(), {}, "corr-error", timeout_s=0.1,
        )
    except RuntimeError as exc:
        assert "bad predicate" in str(exc)
    else:
        raise AssertionError("expected predicate exception to propagate")
    finally:
        runtime._shutdown_where_eval_executor()


@pytest.mark.asyncio
async def test_where_eval_timeout_does_not_block_event_loop():
    runtime = DeviceRuntime(device_id="where-nonblocking-test")
    runtime._logger = Mock()
    ticked = asyncio.Event()

    async def _tick_while_predicate_runs():
        await asyncio.sleep(0.005)
        ticked.set()

    ticker = asyncio.create_task(_tick_while_predicate_runs())
    result = await runtime._evaluate_where_with_timeout(
        _SlowPredicate(), {}, "corr-nonblocking", timeout_s=0.03,
    )

    assert result is False
    assert ticked.is_set()
    await ticker
    runtime._shutdown_where_eval_executor()


@pytest.mark.asyncio
async def test_where_eval_uses_bounded_executor_for_timeouts():
    runtime = DeviceRuntime(device_id="where-bounded-test")
    runtime._logger = Mock()
    started_threads = set()
    release = threading.Event()

    class _BlockingPredicate:
        def evaluate(self, context):
            started_threads.add(threading.current_thread().name)
            release.wait(timeout=1.0)
            return True

    try:
        results = await asyncio.gather(
            *[
                runtime._evaluate_where_with_timeout(
                    _BlockingPredicate(), {}, f"corr-bounded-{i}", timeout_s=0.01,
                )
                for i in range(runtime._WHERE_EVAL_MAX_WORKERS * 2)
            ]
        )

        assert results == [False] * (runtime._WHERE_EVAL_MAX_WORKERS * 2)
        assert len(started_threads) <= runtime._WHERE_EVAL_MAX_WORKERS
    finally:
        release.set()
        runtime._shutdown_where_eval_executor()


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
