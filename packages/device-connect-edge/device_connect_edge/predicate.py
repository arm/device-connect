# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""CEL ``where`` predicate evaluator for self-election at the edge.

A ``where`` predicate is a CEL (Common Expression Language) expression that
each candidate device evaluates against its own context to decide whether
to execute a fan-out call. The predicate sees four top-level variables:

    identity     device-local identity dict (device_id, device_type, ...)
    labels       device labels (the same labels selectors filter on)
    status       device status (heartbeat-updated: location, availability,
                 battery, online, ...)
    bindings     shared payload supplied by the caller (selection masks,
                 thresholds, lookup tables)

Examples::

    battery > 50
    labels.category == "camera" && status.battery > 50
    mask[seat_row][seat_col] == 1
    bindings.threshold < status.temperature

CEL is sandboxed by construction: no I/O, no filesystem, no exec. This
module wraps `cel-python` with lazy import so device-connect-edge does
not require it as a hard dependency. Install with the optional
``[predicate]`` extra::

    pip install device-connect-edge[predicate]

The evaluator is shared by the dispatcher (validates the expression
before broadcast) and the device runtime (evaluates per-call to decide
whether to execute the fan-out).
"""

from __future__ import annotations

from typing import Any, Mapping


class PredicateCompileError(ValueError):
    """Raised when a ``where`` expression fails to compile.

    Carries the original cel-python error chained so callers can drill in
    if they need the exact parse position.
    """


class PredicateEvalError(RuntimeError):
    """Raised when an otherwise-valid predicate fails at evaluation time.

    Typical causes: missing context key, type mismatch (e.g. comparing a
    string to an int), or arithmetic overflow.
    """


# Lazy import: ``cel-python`` is an optional extra. Importers of this module
# pay no cost unless they actually compile a predicate.
def _require_celpy():
    try:
        import celpy  # type: ignore[import-not-found]
        return celpy
    except ImportError as e:
        raise PredicateCompileError(
            "where predicates require the 'cel-python' package; "
            "install with the [predicate] extra: "
            "pip install 'device-connect-edge[predicate]'"
        ) from e


def _to_cel(value: Any) -> Any:
    """Recursively wrap a Python value as the matching CEL type.

    Native Python ints, strings, dicts, and lists arrive at the boundary
    untyped; cel-python's evaluator expects its own typed wrappers
    (``IntType``, ``MapType``, ``ListType``, ...). We wrap once at the
    top of evaluation rather than asking callers to import celtypes.
    """
    celpy = _require_celpy()
    ct = celpy.celtypes
    if value is None:
        return None
    if isinstance(value, bool):
        return ct.BoolType(value)
    if isinstance(value, int):
        return ct.IntType(value)
    if isinstance(value, float):
        return ct.DoubleType(value)
    if isinstance(value, str):
        return ct.StringType(value)
    if isinstance(value, (bytes, bytearray)):
        return ct.BytesType(bytes(value))
    if isinstance(value, Mapping):
        return ct.MapType({
            ct.StringType(str(k)): _to_cel(v) for k, v in value.items()
        })
    if isinstance(value, (list, tuple)):
        return ct.ListType([_to_cel(v) for v in value])
    # Fallback: stringify. Rare; happens for custom objects in the context.
    return ct.StringType(str(value))


class WherePredicate:
    """A compiled ``where`` predicate, ready to evaluate against device context.

    Compile once (typically at the dispatcher when the call comes in or at
    the edge when the broadcast envelope is received), then evaluate once
    per candidate. Predicates are stateless and safe to reuse across calls.
    """

    __slots__ = ("expression", "_program")

    def __init__(self, expression: str, _program: Any):
        self.expression = expression
        self._program = _program

    def evaluate(self, context: Mapping[str, Any]) -> bool:
        """Return ``True`` if the predicate holds for ``context``.

        ``context`` should be a flat mapping of variable name to Python
        value. Common keys: ``identity``, ``labels``, ``status``,
        ``bindings``. Missing keys are not auto-defaulted; if the
        predicate references one, the call raises PredicateEvalError so
        the caller can decide between fail-open and fail-closed.
        """
        celpy = _require_celpy()
        cel_context = {k: _to_cel(v) for k, v in context.items()}
        try:
            result = self._program.evaluate(cel_context)
        except celpy.CELEvalError as e:
            raise PredicateEvalError(
                f"failed to evaluate where {self.expression!r}: {e}"
            ) from e
        return bool(result)


def compile_where(expression: str) -> WherePredicate:
    """Compile a ``where`` expression into a reusable :class:`WherePredicate`.

    Raises :class:`PredicateCompileError` if cel-python is not installed
    or the expression is malformed.
    """
    celpy = _require_celpy()
    if not isinstance(expression, str):
        raise PredicateCompileError(
            f"where expression must be a string, got {type(expression).__name__}"
        )
    if not expression.strip():
        raise PredicateCompileError("where expression must be non-empty")
    env = celpy.Environment()
    try:
        ast = env.compile(expression)
    except Exception as e:
        # cel-python surfaces parse errors via several exception classes
        # depending on the failure mode (lark.UnexpectedToken, ValueError,
        # CELParseError). Catch broadly and rewrap so callers only see
        # PredicateCompileError.
        raise PredicateCompileError(
            f"failed to compile where {expression!r}: {e}"
        ) from e
    program = env.program(ast)
    return WherePredicate(expression=expression, _program=program)
