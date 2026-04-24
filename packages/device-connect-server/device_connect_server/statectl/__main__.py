# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Entry point for running statectl as a module.

Usage:
    python -m device_connect_server.statectl get experiments/EXP-001
    python -m device_connect_server.statectl list
    python -m device_connect_server.statectl --help
"""

from device_connect_server.statectl.cli import main

if __name__ == "__main__":
    main()
