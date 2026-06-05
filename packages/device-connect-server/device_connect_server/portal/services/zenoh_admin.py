# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Zenoh router lifecycle — config reload requested via a signal file.

The Zenoh ACL cannot be hot-reloaded (it is read once at startup), so a
config change requires restarting the router. Rather than give the
web-facing portal access to the Docker socket (a host-takeover lever),
the portal only *requests* a reload by writing a token to a shared
signal file. A tiny privileged ``zenoh-reloader`` sidecar — the only
thing in the stack with the Docker socket — watches that file, debounces
bursts, and restarts the one named router container. The portal holds no
Docker access at all.
"""

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: Shared file the portal touches to request a router restart. The
#: ``zenoh-reloader`` sidecar watches this path on a shared volume.
RELOAD_SIGNAL_PATH = os.environ.get("ZENOH_RELOAD_SIGNAL", "/reload/request")


async def reload_zenoh() -> dict:
    """Request a Zenoh router reload by bumping the shared signal file.

    Non-blocking: the sidecar performs the (debounced) restart
    asynchronously. The ACL change takes effect once the router has
    restarted. Returns ``{success, message}``.
    """
    signal = Path(RELOAD_SIGNAL_PATH)
    try:
        signal.parent.mkdir(parents=True, exist_ok=True)
        # A strictly increasing token; the sidecar restarts when it
        # changes. ns precision avoids collisions on rapid successive
        # requests (which the sidecar coalesces into one restart).
        signal.write_text(str(time.time_ns()))
        logger.info("Requested Zenoh reload via %s", signal)
        return {
            "success": True,
            "message": "Zenoh reload requested; the reloader sidecar will "
                       "restart the router (debounced).",
        }
    except OSError as e:
        return {
            "success": False,
            "message": f"Could not write reload signal {signal}: {e}",
        }
