# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""``statectl`` selector-driven operations verbs.

Thin wrappers around the agent-tools ``invoke`` / ``invoke_many`` /
``broadcast`` / ``subscribe`` / ``await_replies`` functions so operators
can fire selector-driven calls from a shell.
"""
from __future__ import annotations

import json
import os
from typing import Any


def _connect(broker: str | None) -> None:
    """Connect to the messaging backend using the same env-or-broker rules
    as devctl's selector verbs."""
    from device_connect_agent_tools import connect

    if broker:
        connect(nats_url=broker)
    else:
        nats_url = os.getenv("NATS_URL") or os.getenv("DEVICE_CONNECT_NATS_URL")
        if nats_url:
            connect(nats_url=nats_url)
        else:
            connect()


def _parse_param_kv(values: list[str] | None) -> dict[str, Any]:
    """Parse ``--param k=v`` repeated args into a function-params dict.

    Values that look like JSON (``[...]``, ``{...}``, numbers, ``true`` /
    ``false`` / ``null``) are decoded; everything else stays a string. This
    matches what an operator would expect when typing
    ``--param resolution=1080p --param tags='["a","b"]'``.
    """
    out: dict[str, Any] = {}
    for entry in values or []:
        if "=" not in entry:
            raise ValueError(f"--param must be 'k=v', got {entry!r}")
        k, _, v = entry.partition("=")
        k = k.strip()
        if not k:
            raise ValueError(f"--param has empty key in {entry!r}")
        v_stripped = v.strip()
        # JSON-decode obvious JSON-shaped values; fall back to raw string.
        if (
            v_stripped.startswith(("[", "{", '"'))
            or v_stripped in ("true", "false", "null")
            or _looks_numeric(v_stripped)
        ):
            try:
                out[k] = json.loads(v_stripped)
                continue
            except json.JSONDecodeError:
                pass
        out[k] = v
    return out


def _looks_numeric(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _pretty(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, default=str)


# -- verbs ----------------------------------------------------------


def run_invoke(args: Any) -> int:
    from device_connect_agent_tools import disconnect, invoke

    _connect(getattr(args, "broker", None))
    try:
        result = invoke(
            args.selector,
            params=_parse_param_kv(args.param),
            llm_reasoning=args.reason,
        )
        print(_pretty(result))
        return 0 if result.get("success") else 1
    finally:
        try:
            disconnect()
        except Exception:  # pragma: no cover
            pass


def run_invoke_many(args: Any) -> int:
    from device_connect_agent_tools import disconnect, invoke_many

    _connect(getattr(args, "broker", None))
    try:
        result = invoke_many(
            args.selector,
            params=_parse_param_kv(args.param),
            timeout=float(args.timeout),
            max_concurrency=int(args.max_concurrency),
            llm_reasoning=args.reason,
        )
        print(_pretty(result))
        # Exit non-zero on a top-level error OR when any target failed, so
        # shell pipelines can detect partial failure without parsing JSON.
        if "error" in result:
            return 1
        if result.get("failed", 0) > 0:
            return 3
        return 0
    finally:
        try:
            disconnect()
        except Exception:  # pragma: no cover
            pass


def run_broadcast(args: Any) -> int:
    from device_connect_agent_tools import broadcast, disconnect

    bindings = None
    if args.bindings:
        try:
            bindings = json.loads(args.bindings)
        except json.JSONDecodeError as e:
            print(f"--bindings must be valid JSON: {e}")
            return 2

    _connect(getattr(args, "broker", None))
    try:
        result = broadcast(
            args.selector,
            params=_parse_param_kv(args.param),
            where=args.where,
            bindings=bindings,
            fire_at=float(args.fire_at) if args.fire_at is not None else None,
            on_late=args.on_late,
            llm_reasoning=args.reason,
        )
        print(_pretty(result))
        return 0 if "error" not in result else 1
    finally:
        try:
            disconnect()
        except Exception:  # pragma: no cover
            pass


def run_subscribe(args: Any) -> int:
    """Stream events / replies for ``args.selector`` to stdout.

    Each message is printed as one JSON line so the output can be piped
    into ``jq`` or grep. Runs until ``--timeout`` of idle silence elapses
    or ``--until`` messages have been printed (whichever comes first).
    Exit codes:
        0   one or more messages were printed
        4   idle-timeout reached with zero messages
        130 interrupted with Ctrl-C
    """
    from device_connect_agent_tools import disconnect, subscribe

    _connect(getattr(args, "broker", None))
    count = 0
    try:
        with subscribe(args.selector) as sub:
            try:
                for msg in sub.iter(
                    timeout=float(args.timeout), poll_interval=0.05,
                ):
                    print(json.dumps(msg, default=str))
                    count += 1
                    if args.until is not None and count >= int(args.until):
                        break
            except KeyboardInterrupt:
                # Clean exit on Ctrl-C: the ``with`` block tears the
                # subscription down before this returns.
                return 130
        return 0 if count > 0 else 4
    finally:
        try:
            disconnect()
        except Exception:  # pragma: no cover
            pass


def run_await(args: Any) -> int:
    from device_connect_agent_tools import await_replies, disconnect

    _connect(getattr(args, "broker", None))
    try:
        replies = await_replies(
            args.correlation_id,
            timeout=float(args.timeout),
            until=int(args.until) if args.until is not None else None,
        )
        print(_pretty(replies))
        return 0
    finally:
        try:
            disconnect()
        except Exception:  # pragma: no cover
            pass


# -- parser wiring --------------------------------------------------


def register_subparsers(sub: Any) -> None:
    """Attach the operation subparsers to a statectl parser."""
    p = sub.add_parser("invoke", help="Call exactly one function on one device")
    p.add_argument("selector", help="Function-scoped selector")
    p.add_argument(
        "--param", action="append", default=[],
        help="Function param as k=v (repeatable; JSON values decoded)",
    )
    p.add_argument("--reason", default=None, help="LLM reasoning")
    p.add_argument("--broker", default=None, help="Messaging broker URL")

    p = sub.add_parser(
        "invoke-many", help="Fan out a call over a selector-resolved set",
    )
    p.add_argument("selector", help="Function-scoped selector")
    p.add_argument(
        "--param", action="append", default=[],
        help="Function param as k=v (repeatable; JSON values decoded)",
    )
    p.add_argument("--timeout", default=30.0, help="Per-target timeout (s)")
    p.add_argument(
        "--max-concurrency", default=32, dest="max_concurrency",
        help="Parallel worker cap",
    )
    p.add_argument("--reason", default=None, help="LLM reasoning")
    p.add_argument("--broker", default=None, help="Messaging broker URL")

    p = sub.add_parser(
        "broadcast",
        help="Async fan-out; returns correlation_id",
    )
    p.add_argument("selector", help="Function-scoped selector")
    p.add_argument(
        "--param", action="append", default=[],
        help="Function param as k=v (repeatable; JSON values decoded)",
    )
    p.add_argument(
        "--where", default=None,
        help="CEL predicate evaluated at the edge per candidate",
    )
    p.add_argument(
        "--bindings", default=None,
        help="JSON-encoded bindings dict (shared payload for the predicate)",
    )
    p.add_argument(
        "--fire-at", default=None, dest="fire_at",
        help="Wall-clock epoch seconds for synchronized fan-out",
    )
    p.add_argument(
        "--on-late", choices=["skip", "fire"], default="skip", dest="on_late",
        help="Policy when fire_at deadline has passed (default: skip)",
    )
    p.add_argument("--reason", default=None, help="LLM reasoning")
    p.add_argument("--broker", default=None, help="Messaging broker URL")

    p = sub.add_parser(
        "subscribe", help="Stream events or broadcast replies to stdout",
    )
    p.add_argument(
        "selector",
        help="Event selector or 'correlation:<id>' for broadcast replies",
    )
    p.add_argument(
        "--timeout", default=10.0,
        help="Idle-silence timeout per message (s; resets on each arrival)",
    )
    p.add_argument(
        "--until", default=None,
        help="Stop after this many messages are printed",
    )
    p.add_argument("--broker", default=None, help="Messaging broker URL")

    p = sub.add_parser(
        "await", help="Collect replies for a broadcast correlation_id",
    )
    p.add_argument("correlation_id", help="Correlation id returned by broadcast")
    p.add_argument("--timeout", default=10.0, help="Overall timeout (s)")
    p.add_argument(
        "--until", default=None,
        help="Stop after this many replies have been collected",
    )
    p.add_argument("--broker", default=None, help="Messaging broker URL")
