# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the CEL ``where`` predicate evaluator.

These tests require the ``[predicate]`` extra (cel-python). They are
skipped automatically when cel-python is not installed so the rest of
the edge test suite stays runnable on minimal installs.
"""
from __future__ import annotations

import pytest

celpy = pytest.importorskip("celpy")

from device_connect_edge.predicate import (
    PredicateCompileError,
    PredicateEvalError,
    WherePredicate,
    compile_where,
)


# -- compile_where --------------------------------------------------


class TestCompile:
    def test_simple_comparison_compiles(self):
        p = compile_where("battery > 50")
        assert isinstance(p, WherePredicate)
        assert p.expression == "battery > 50"

    def test_boolean_combination_compiles(self):
        p = compile_where("a > 1 && b < 10 || c == 'x'")
        assert isinstance(p, WherePredicate)

    def test_array_indexing_compiles(self):
        p = compile_where("mask[row][col] == 1")
        assert isinstance(p, WherePredicate)

    def test_label_dot_access_compiles(self):
        p = compile_where("labels.category == 'camera'")
        assert isinstance(p, WherePredicate)

    def test_empty_expression_rejected(self):
        with pytest.raises(PredicateCompileError):
            compile_where("")
        with pytest.raises(PredicateCompileError):
            compile_where("   ")

    def test_non_string_rejected(self):
        with pytest.raises(PredicateCompileError):
            compile_where(123)  # type: ignore[arg-type]

    def test_malformed_expression_rejected(self):
        with pytest.raises(PredicateCompileError) as exc:
            compile_where("a > > b")
        assert "failed to compile" in str(exc.value)


# -- evaluate -------------------------------------------------------


class TestEvaluate:
    def test_truthy_comparison(self):
        p = compile_where("battery > 50")
        assert p.evaluate({"battery": 80}) is True
        assert p.evaluate({"battery": 30}) is False

    def test_label_match(self):
        p = compile_where("labels.category == 'camera'")
        assert p.evaluate({"labels": {"category": "camera"}}) is True
        assert p.evaluate({"labels": {"category": "robot"}}) is False

    def test_2d_mask_indexing(self):
        # The mask-indexing case is the deciding example for picking CEL
        # over JSONLogic; keep it as a regression guard.
        p = compile_where("mask[row][col] == 1")
        ctx = {
            "mask": [[0, 1, 0], [1, 0, 0]],
            "row": 0,
            "col": 1,
        }
        assert p.evaluate(ctx) is True
        ctx["col"] = 0
        assert p.evaluate(ctx) is False

    def test_combined_label_and_status(self):
        p = compile_where("labels.category == 'camera' && status.battery > 50")
        ctx = {
            "labels": {"category": "camera"},
            "status": {"battery": 80},
        }
        assert p.evaluate(ctx) is True
        ctx["status"]["battery"] = 30
        assert p.evaluate(ctx) is False
        ctx["labels"]["category"] = "robot"
        ctx["status"]["battery"] = 80
        assert p.evaluate(ctx) is False

    def test_bindings_and_status_compose(self):
        p = compile_where("status.temperature > bindings.threshold")
        ctx = {
            "status": {"temperature": 75.5},
            "bindings": {"threshold": 70.0},
        }
        assert p.evaluate(ctx) is True

    def test_string_in_list(self):
        p = compile_where("labels.category in ['camera', 'inference']")
        assert p.evaluate({"labels": {"category": "camera"}}) is True
        assert p.evaluate({"labels": {"category": "robot"}}) is False

    def test_missing_variable_raises_eval_error(self):
        p = compile_where("status.battery > 50")
        with pytest.raises(PredicateEvalError):
            p.evaluate({})

    def test_type_mismatch_raises_eval_error(self):
        p = compile_where("battery > 50")
        with pytest.raises(PredicateEvalError):
            p.evaluate({"battery": "not a number"})

    def test_evaluator_is_reusable(self):
        # Compile once, evaluate against many contexts. Reusability is the
        # property that lets callers compile broadcast envelopes once at
        # the dispatcher and ship them to N targets.
        p = compile_where("battery > 50")
        results = [p.evaluate({"battery": v}) for v in (10, 50, 51, 100)]
        assert results == [False, False, True, True]
