# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Discovery provider protocol for unified device discovery.

Defines the ``DiscoveryProvider`` protocol that both D2D (local presence)
and Infra (registry service) discovery backends implement. This allows
agent-facing code to discover and query devices without knowing which
backend is in use.

Implementations:
- ``D2DRegistry`` in :mod:`device_connect_edge.discovery`
- ``RegistryClient`` in :mod:`device_connect_edge.registry_client`
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class DiscoveryProvider(Protocol):
    """Protocol for device discovery backends.

    Minimal interface: ``list_devices`` and ``get_device``. Both D2D
    (presence-based, in-memory) and Infra (registry service over
    messaging) implementations conform to this protocol.
    """

    async def list_devices(
        self,
        *,
        device_type: Optional[str] = None,
        location: Optional[str] = None,
        capabilities: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """List available devices, optionally filtered.

        Args:
            device_type: Filter by device type (e.g., "camera", "robot").
            location: Filter by device location.
            capabilities: Filter by required capabilities.

        Returns:
            List of device dictionaries with full registration data.
        """
        ...

    async def get_device(
        self,
        device_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Get a specific device by ID.

        Args:
            device_id: Device identifier.

        Returns:
            Device dictionary, or ``None`` if not found.
        """
        ...
