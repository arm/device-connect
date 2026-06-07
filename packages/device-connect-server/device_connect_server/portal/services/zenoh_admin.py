# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Zenoh router lifecycle -- config reload requested via a signal file.

The Zenoh ACL is read once at router startup and cannot be hot-reloaded, so
a config change requires restarting the router. Rather than give the
web-facing portal the Docker socket (a host-takeover lever), the portal only
*requests* a reload by writing a token to a shared signal file; a tiny
privileged ``zenoh-reloader`` sidecar -- the only thing with the socket --
debounces and restarts the one named router container.

Reload is *gated on an actual config change*. Under the per-tenant-CN model
(see docs/zenoh-per-tenant-cn.md) device provisioning and revocation do NOT
change ``zenoh-config.json5`` -- only tenant creation/deletion does -- so we
hash the config and skip the (disruptive, fleet-wide) restart when nothing
changed. This is what makes per-device operations reload-free.
"""

import hashlib
import logging
import os
import time
from pathlib import Path

from .. import config

logger = logging.getLogger(__name__)

#: Shared file the portal touches to request a router restart. The
#: ``zenoh-reloader`` sidecar watches this path on a shared volume.
RELOAD_SIGNAL_PATH = os.environ.get("ZENOH_RELOAD_SIGNAL", "/reload/request")


def _config_path() -> Path:
    return config.SECURITY_INFRA_DIR / "zenoh-config.json5"


def _hash_path() -> Path:
    # Lives next to the config (portal-only); records the hash of the config
    # the router was last (re)started with, so we can detect real changes.
    return config.SECURITY_INFRA_DIR / ".zenoh-config-reloaded-sha256"


def _current_hash() -> str | None:
    try:
        return hashlib.sha256(_config_path().read_bytes()).hexdigest()
    except OSError:
        return None


def mark_reloaded(config_hash: str | None = None) -> None:
    """Record the config hash the router is now running.

    Called after bootstrap (the router starts with the freshly generated
    config) so the first device provision -- which does not change the
    config -- correctly skips the reload instead of triggering one spurious
    restart.
    """
    config_hash = config_hash or _current_hash()
    if not config_hash:
        return
    try:
        _hash_path().write_text(config_hash)
    except OSError:
        logger.warning("Could not record reloaded config hash")


async def reload_zenoh() -> dict:
    """Request a Zenoh router reload, but only if the config actually changed.

    Returns ``{success, message, reloaded}``. When the config is unchanged
    (the common case for device provisioning/revocation under per-tenant-CN)
    the restart is skipped and ``reloaded`` is False.
    """
    cur = _current_hash()
    try:
        last = _hash_path().read_text().strip()
    except OSError:
        last = None

    if cur is not None and cur == last:
        logger.info("Zenoh config unchanged; reload skipped (no restart)")
        return {
            "success": True,
            "reloaded": False,
            "message": "No ACL/config change; router reload skipped "
                       "(per-tenant-CN: device provisioning/revocation needs no restart).",
        }

    signal = Path(RELOAD_SIGNAL_PATH)
    try:
        signal.parent.mkdir(parents=True, exist_ok=True)
        # A strictly increasing token; the sidecar restarts when it changes
        # and debounces a burst into a single restart.
        signal.write_text(str(time.time_ns()))
        if cur is not None:
            mark_reloaded(cur)
        logger.info("Requested Zenoh reload via %s", signal)
        return {
            "success": True,
            "reloaded": True,
            "message": "Zenoh reload requested; the reloader sidecar will "
                       "restart the router (debounced).",
        }
    except OSError as e:
        return {
            "success": False,
            "reloaded": False,
            "message": f"Could not write reload signal {signal}: {e}",
        }
