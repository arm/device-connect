# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""``devctl`` selector-driven discovery verbs.

Thin wrappers around ``device_connect_agent_tools.discover`` and
``discover_labels`` so operators can drive the same selector grammar
from a shell.
"""
from __future__ import annotations

import json
import os
from typing import Any


def _connect(broker: str | None) -> None:
    """Best-effort connect to the messaging backend.

    Reuses ``DEVICE_CONNECT_*`` and ``NATS_URL`` env vars when ``broker`` is
    not given. Kept as a thin wrapper so all CLI verbs share the same
    connect-or-fail semantics.
    """
    from device_connect_agent_tools import connect

    if broker:
        connect(nats_url=broker)
    else:
        nats_url = os.getenv("NATS_URL") or os.getenv("DEVICE_CONNECT_NATS_URL")
        if nats_url:
            connect(nats_url=nats_url)
        else:
            connect()


def _pretty(data: Any) -> str:
    """Render a JSON payload for terminal output."""
    return json.dumps(data, indent=2, sort_keys=True, default=str)


def run_discover(args: Any) -> int:
    """Execute ``devctl discover "<selector>"``."""
    from device_connect_agent_tools import disconnect, discover

    _connect(getattr(args, "broker", None))
    try:
        result = discover(
            args.selector,
            offset=int(args.offset or 0),
            limit=int(args.limit or 200),
        )
        print(_pretty(result))
        return 0 if "error" not in result else 1
    finally:
        try:
            disconnect()
        except Exception:  # pragma: no cover
            pass


def run_discover_labels(args: Any) -> int:
    """Execute ``devctl discover-labels [--key K]``."""
    from device_connect_agent_tools import disconnect, discover_labels

    _connect(getattr(args, "broker", None))
    try:
        result = discover_labels(
            key=args.key,
            offset=int(args.offset or 0),
            limit=int(args.limit or 50),
        )
        print(_pretty(result))
        return 0 if "error" not in result else 1
    finally:
        try:
            disconnect()
        except Exception:  # pragma: no cover
            pass


def register_subparsers(sub: Any) -> None:
    """Attach the discover / discover-labels subparsers to a devctl parser."""
    p = sub.add_parser(
        "discover",
        help="Resolve a selector to devices, functions, or events",
    )
    p.add_argument("selector", help="Selector expression (e.g. 'device(category:camera)')")
    p.add_argument("--broker", default=None, help="Messaging broker URL")
    p.add_argument("--offset", type=int, default=0, help="Pagination offset")
    p.add_argument("--limit", type=int, default=200, help="Page size")

    p = sub.add_parser(
        "discover-labels",
        help="Browse fleet label vocabulary",
    )
    p.add_argument(
        "--key", default=None,
        help="Axis-qualified label key (e.g. 'device.location') for per-key pagination",
    )
    p.add_argument("--broker", default=None, help="Messaging broker URL")
    p.add_argument("--offset", type=int, default=0, help="Pagination offset")
    p.add_argument("--limit", type=int, default=50, help="Page size")
