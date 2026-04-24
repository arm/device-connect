# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Device control CLI for Device Connect.

This module provides a command-line interface for interacting with
the device registry, commissioning devices, and performing device operations.

CLI Usage:
    python -m device_connect_server.devctl list [--compact]
    python -m device_connect_server.devctl register --id myDevice [--keepalive]
    python -m device_connect_server.devctl discover [--timeout 5]
    python -m device_connect_server.devctl commission <device_id> --pin 1234-5678
    python -m device_connect_server.devctl interactive
"""

from device_connect_server.devctl.cli import main

__all__ = ["main"]
