"""CLI for inspecting and managing etcd state in Device Connect.

A Python-native replacement for etcdctl, tailored for the Device Connect
key namespaces (/device-connect/state/, /device-connect/locks/, /device-connect/devices/).

Usage:
    python -m device_connect_server.statectl get experiments/EXP-001
    python -m device_connect_server.statectl list experiments/
    python -m device_connect_server.statectl list --raw /device-connect/
    python -m device_connect_server.statectl set experiments/EXP-001 '{"status":"done"}'
    python -m device_connect_server.statectl delete experiments/EXP-001
    python -m device_connect_server.statectl watch experiments/ --prefix
    python -m device_connect_server.statectl locks
    python -m device_connect_server.statectl stats
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

# Lazy import — etcd3gw may not be installed
try:
    import etcd3gw
    ETCD3GW_AVAILABLE = True
except ImportError:
    ETCD3GW_AVAILABLE = False
    etcd3gw = None

ETCD_HOST = os.getenv("ETCD_HOST", "localhost")
ETCD_PORT = int(os.getenv("ETCD_PORT", "2379"))
DEFAULT_PREFIX = "/device-connect/state/"
LOCK_PREFIX = "/device-connect/locks/"

# Known namespaces for the stats command
KNOWN_NAMESPACES = [
    ("/device-connect/state/experiments/", "experiments"),
    ("/device-connect/state/device_locks/", "device_locks"),
    ("/device-connect/state/plates/", "plates"),
    ("/device-connect/devices/", "devices (registry)"),
    ("/device-connect/locks/", "locks"),
]


# ── helpers ──────────────────────────────────────────────────────────

def _require_etcd3gw():
    """Exit with a helpful message if etcd3gw is not installed."""
    if not ETCD3GW_AVAILABLE:
        print(
            "etcd3gw is required for statectl.\n"
            "Install with: pip install etcd3gw",
            file=sys.stderr,
        )
        sys.exit(1)


def _get_client(host: str, port: int):
    """Create an etcd3gw client."""
    _require_etcd3gw()
    return etcd3gw.client(host=host, port=port)


def _kv_key(kv: dict) -> str:
    """Extract the key string from an etcd3gw KV metadata dict."""
    raw = kv.get("key", "")
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    try:
        return base64.b64decode(raw).decode("utf-8")
    except Exception:
        return raw if isinstance(raw, str) else str(raw)


def _resolve_key(key: str, prefix: str, raw: bool) -> str:
    """Build the full etcd key, prepending prefix unless --raw."""
    if raw:
        return key
    return f"{prefix}{key}"


def _decode_value(raw) -> Any:
    """Decode an etcd value: try JSON first, fall back to string."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def _print_json(data: Any) -> None:
    """Pretty-print data as JSON."""
    print(json.dumps(data, indent=2, default=str))


def _print_table(rows: List[Tuple[str, Any]]) -> None:
    """Print key-value rows as an aligned table."""
    if not rows:
        print("(empty)")
        return
    max_key = max(len(k) for k, _ in rows)
    max_key = max(max_key, 3)  # minimum width for "KEY"
    print(f"{'KEY'.ljust(max_key)}  VALUE")
    print("-" * (max_key + 62))
    for key, val in sorted(rows):
        val_str = json.dumps(val, default=str) if not isinstance(val, str) else val
        if len(val_str) > 60:
            val_str = val_str[:57] + "..."
        print(f"{key.ljust(max_key)}  {val_str}")


def _format_output(data: Dict[str, Any], fmt: str) -> None:
    """Format and print a key-value mapping."""
    if fmt == "json":
        _print_json(data)
    elif fmt == "compact":
        for key in sorted(data):
            print(key)
    elif fmt == "table":
        _print_table(list(data.items()))


# ── commands ─────────────────────────────────────────────────────────

async def cmd_get(client, args) -> None:
    """Get value for a single key."""
    full_key = _resolve_key(args.key, args.prefix, args.raw)
    loop = asyncio.get_event_loop()

    if args.verbose:
        results = await loop.run_in_executor(
            None, lambda: client.get(full_key, metadata=True)
        )
        if not results:
            print(f"Key not found: {full_key}", file=sys.stderr)
            sys.exit(1)
        value, kv = results[0]
        parsed = _decode_value(value)
        output = {
            "key": full_key,
            "value": parsed,
            "create_revision": kv.get("create_revision"),
            "mod_revision": kv.get("mod_revision"),
            "version": kv.get("version"),
        }
    else:
        results = await loop.run_in_executor(None, client.get, full_key)
        if not results:
            print(f"Key not found: {full_key}", file=sys.stderr)
            sys.exit(1)
        parsed = _decode_value(results[0])
        output = parsed

    _print_json(output)


async def cmd_list(client, args) -> None:
    """List keys matching a prefix."""
    prefix_filter = args.prefix_filter or ""
    full_prefix = _resolve_key(prefix_filter, args.prefix, args.raw)
    if not full_prefix:
        full_prefix = "/"
    loop = asyncio.get_event_loop()

    results = await loop.run_in_executor(
        None, client.get_prefix, full_prefix
    )

    data: Dict[str, Any] = {}
    for value, kv in results:
        raw_key = _kv_key(kv)
        parsed = _decode_value(value)
        if args.verbose:
            parsed = {
                "value": parsed,
                "create_revision": kv.get("create_revision"),
                "mod_revision": kv.get("mod_revision"),
                "version": kv.get("version"),
            }
        data[raw_key] = parsed

    if not data:
        print(f"No keys found under: {full_prefix}", file=sys.stderr)
        return

    _format_output(data, args.output_format)


async def cmd_set(client, args) -> None:
    """Set a key to a JSON value."""
    full_key = _resolve_key(args.key, args.prefix, args.raw)

    # Validate JSON
    try:
        parsed = json.loads(args.value)
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON: {exc}", file=sys.stderr)
        sys.exit(1)

    encoded = json.dumps(parsed)
    loop = asyncio.get_event_loop()

    if args.ttl is not None:
        lease = await loop.run_in_executor(
            None, lambda: client.lease(ttl=args.ttl)
        )
        await loop.run_in_executor(
            None, lambda: client.put(full_key, encoded, lease=lease)
        )
        print(f"Set {full_key} (TTL={args.ttl}s)")
    else:
        await loop.run_in_executor(None, client.put, full_key, encoded)
        print(f"Set {full_key}")


async def cmd_delete(client, args) -> None:
    """Delete a key or all keys under a prefix."""
    full_key = _resolve_key(args.key, args.prefix, args.raw)
    loop = asyncio.get_event_loop()

    if args.delete_prefix:
        deleted = await loop.run_in_executor(
            None, client.delete_prefix, full_key
        )
        if deleted:
            print(f"Deleted all keys under {full_key}")
        else:
            print(f"No keys found under: {full_key}", file=sys.stderr)
    else:
        deleted = await loop.run_in_executor(None, client.delete, full_key)
        if deleted:
            print(f"Deleted {full_key}")
        else:
            print(f"Key not found: {full_key}", file=sys.stderr)


async def cmd_watch(client, args) -> None:
    """Watch for changes on a key or prefix."""
    full_key = _resolve_key(args.key, args.prefix, args.raw)
    is_prefix = args.watch_prefix
    loop = asyncio.get_event_loop()

    label = "prefix" if is_prefix else "key"
    print(f"Watching {label}: {full_key}", file=sys.stderr)
    print("Press Ctrl-C to stop\n", file=sys.stderr)

    def _start_watch():
        if is_prefix:
            return client.watch_prefix(full_key)
        return client.watch(full_key)

    events_iterator, cancel = await loop.run_in_executor(None, _start_watch)

    def _iterate():
        try:
            for event in events_iterator:
                key = event.get("kv", {}).get("key", "")
                try:
                    key = base64.b64decode(key).decode("utf-8")
                except Exception:
                    pass
                event_type = event.get("type", "PUT")
                ts = datetime.now(timezone.utc).isoformat()

                raw_value = event.get("kv", {}).get("value")
                if raw_value:
                    try:
                        value = _decode_value(
                            base64.b64decode(raw_value)
                        )
                    except Exception:
                        value = raw_value
                else:
                    value = None

                record = {
                    "type": event_type,
                    "key": key,
                    "value": value,
                    "ts": ts,
                }
                print(json.dumps(record, default=str), flush=True)
        except Exception:
            pass  # cancelled or connection closed

    try:
        await loop.run_in_executor(None, _iterate)
    except KeyboardInterrupt:
        pass
    finally:
        cancel()


async def cmd_locks(client, args) -> None:
    """List currently held locks."""
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(
        None, client.get_prefix, LOCK_PREFIX
    )

    data: Dict[str, Any] = {}
    for value, kv in results:
        raw_key = _kv_key(kv)
        parsed = _decode_value(value)
        data[raw_key] = parsed

    if not data:
        print("No locks currently held.")
        return

    _format_output(data, args.output_format)


async def cmd_stats(client, args) -> None:
    """Show key counts by namespace."""
    loop = asyncio.get_event_loop()

    total = 0
    rows = []
    for ns_prefix, label in KNOWN_NAMESPACES:
        results = list(
            await loop.run_in_executor(None, client.get_prefix, ns_prefix)
        )
        count = len(results)
        total += count
        rows.append((label, count))

    max_label = max(len(label) for label, _ in rows)
    max_label = max(max_label, 9)  # "NAMESPACE"
    print(f"{'NAMESPACE'.ljust(max_label)}  COUNT")
    print("-" * (max_label + 8))
    for label, count in rows:
        print(f"{label.ljust(max_label)}  {count}")
    print("-" * (max_label + 8))
    print(f"{'total'.ljust(max_label)}  {total}")


# ── parser ───────────────────────────────────────────────────────────

def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m device_connect_server.statectl",
        description="Device Connect State Store CLI — inspect and manage etcd state",
    )

    # Global options
    parser.add_argument("--host", default=ETCD_HOST,
                        help=f"etcd host (default: {ETCD_HOST})")
    parser.add_argument("--port", type=int, default=ETCD_PORT,
                        help=f"etcd port (default: {ETCD_PORT})")
    parser.add_argument("--prefix", default=DEFAULT_PREFIX,
                        help=f"Key prefix (default: {DEFAULT_PREFIX})")
    parser.add_argument("--raw", action="store_true",
                        help="Raw key mode — no prefix applied")
    parser.add_argument("--format", choices=["json", "table", "compact"],
                        default="json", dest="output_format",
                        help="Output format (default: json)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show key metadata (revisions, version)")

    sub = parser.add_subparsers(dest="cmd", required=True)

    # get
    p_get = sub.add_parser("get", help="Get value for a key")
    p_get.add_argument("key", help="Key to retrieve")

    # list
    p_list = sub.add_parser("list", help="List keys under a prefix")
    p_list.add_argument("prefix_filter", nargs="?", default="",
                        help="Prefix filter (default: list all)")

    # set
    p_set = sub.add_parser("set", help="Set a key to a JSON value")
    p_set.add_argument("key", help="Key to set")
    p_set.add_argument("value", help="JSON value string")
    p_set.add_argument("--ttl", type=int, default=None,
                        help="TTL in seconds (default: no expiry)")

    # delete
    p_del = sub.add_parser("delete", help="Delete a key or prefix")
    p_del.add_argument("key", help="Key to delete")
    p_del.add_argument("--prefix", action="store_true",
                        dest="delete_prefix",
                        help="Delete all keys under this prefix")

    # watch
    p_watch = sub.add_parser("watch", help="Watch for changes (streaming)")
    p_watch.add_argument("key", help="Key or prefix to watch")
    p_watch.add_argument("--prefix", action="store_true",
                         dest="watch_prefix",
                         help="Treat key as prefix (watch all keys under it)")

    # locks
    sub.add_parser("locks", help="List currently held locks")

    # stats
    sub.add_parser("stats", help="Key counts by namespace")

    return parser


# ── dispatch ─────────────────────────────────────────────────────────

COMMANDS = {
    "get": cmd_get,
    "list": cmd_list,
    "set": cmd_set,
    "delete": cmd_delete,
    "watch": cmd_watch,
    "locks": cmd_locks,
    "stats": cmd_stats,
}


async def _run(args) -> None:
    client = _get_client(args.host, args.port)
    handler = COMMANDS[args.cmd]
    await handler(client, args)


def main():
    parser = create_parser()
    args = parser.parse_args()
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
