# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Registry client for Device Connect.

This module provides the RegistryClient for querying the device registry.
It uses the messaging layer to communicate with the registry service.

Example:
    from device_connect_server.registry import RegistryClient
    from device_connect_edge.messaging import create_client

    async with RegistryClient(messaging_client) as registry:
        devices = await registry.list_devices()
        camera = await registry.get_device("camera-001")
"""
from device_connect_server.registry.client import RegistryClient

__all__ = [
    "RegistryClient",
]
