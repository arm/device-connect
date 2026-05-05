# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Admin-side token management for the agent API.

Run on the portal host (talks directly to etcd; no Bearer token needed).
This is the bootstrap path for minting the first token; further token CRUD
can move to a portal browser UI in Phase 5.

Usage:
    python -m device_connect_server.portalctl.admin_tokens create \\
        --user alice --tenant acme --role user \\
        --scopes devices:read,devices:invoke --label ci-bot
    python -m device_connect_server.portalctl.admin_tokens list [--user U] [--tenant T]
    python -m device_connect_server.portalctl.admin_tokens revoke --token-id <id>
"""

from __future__ import annotations

import argparse
import json
import sys

from ..portal.services import tokens as tokens_svc


def _cmd_create(args) -> int:
    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
    try:
        out = tokens_svc.create_token(
            username=args.user,
            tenant=args.tenant,
            role=args.role,
            scopes=scopes,
            label=args.label or "",
            expires_at=args.expires_at,
        )
    except ValueError as e:
        sys.stderr.write(f"error: {e}\n")
        return 2
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    sys.stderr.write(
        "\nIMPORTANT: the 'token' field is shown ONLY ONCE. "
        "Save it somewhere safe; it cannot be recovered.\n"
    )
    return 0


def _cmd_list(args) -> int:
    rows = tokens_svc.list_tokens(username=args.user, tenant=args.tenant)
    json.dump(rows, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


def _cmd_revoke(args) -> int:
    ok = tokens_svc.revoke_token(args.token_id)
    if not ok:
        sys.stderr.write(f"no token with id {args.token_id}\n")
        return 1
    print(f"revoked {args.token_id}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m device_connect_server.portalctl.admin_tokens",
        description="Admin-side token management (writes directly to etcd).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="mint a new token (secret printed once)")
    c.add_argument("--user", required=True)
    c.add_argument("--tenant", required=True)
    c.add_argument("--role", default="user", choices=["user", "admin"])
    c.add_argument("--scopes", default="devices:read",
                   help="comma-separated scopes")
    c.add_argument("--label", default="")
    c.add_argument("--expires-at", default=None,
                   help="ISO-8601 expiry timestamp (optional)")
    c.set_defaults(func=_cmd_create)

    l = sub.add_parser("list", help="list tokens (excludes secret material)")
    l.add_argument("--user", default=None)
    l.add_argument("--tenant", default=None)
    l.set_defaults(func=_cmd_list)

    r = sub.add_parser("revoke", help="revoke a token by id")
    r.add_argument("--token-id", required=True)
    r.set_defaults(func=_cmd_revoke)

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
