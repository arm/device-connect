# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""State store CLI for Device Connect.

This module provides a command-line interface for inspecting and managing
etcd state — a Python-native replacement for etcdctl.

CLI Usage:
    python -m device_connect_server.statectl get experiments/EXP-001
    python -m device_connect_server.statectl list experiments/
    python -m device_connect_server.statectl list --raw /device-connect/
    python -m device_connect_server.statectl set experiments/EXP-001 '{"status":"done"}'
    python -m device_connect_server.statectl delete experiments/EXP-001
    python -m device_connect_server.statectl watch experiments/ --prefix
    python -m device_connect_server.statectl locks
    python -m device_connect_server.statectl stats
"""

from device_connect_server.statectl.cli import main

__all__ = ["main"]
