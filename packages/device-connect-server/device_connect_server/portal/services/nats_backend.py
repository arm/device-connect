# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""NATS backend — delegates to existing nsc, nats_admin, nats_rpc modules."""

import logging
from pathlib import Path
from typing import Any

from .. import config
from . import nats_admin, nats_rpc, nsc, verification
from .backend import MessagingBackendService

logger = logging.getLogger(__name__)


class NatsBackend(MessagingBackendService):
    """MessagingBackendService implementation for NATS (JWT auth via nsc)."""

    def backend_name(self) -> str:
        return "nats"

    def is_bootstrapped(self) -> bool:
        return nsc.is_bootstrapped()

    async def bootstrap(self, host: str, port: str, **kwargs) -> dict:
        from .backend import _write_backend_choice
        result = await nsc.bootstrap(host, port)
        _write_backend_choice("nats", host, port)
        return result

    async def create_tenant(
        self, tenant: str, num_devices: int, host: str, port: str,
    ) -> list[str]:
        return await nsc.create_tenant(tenant, num_devices, host, port)

    async def add_device(
        self, tenant: str, device_name: str, host: str, port: str,
    ) -> Path:
        return await nsc.add_device(tenant, device_name, host, port)

    async def reload_broker(self) -> dict:
        return await nats_admin.reload_nats()

    async def rpc_invoke(
        self, tenant: str, device_id: str, function: str,
        params: dict, timeout: float = 5.0,
    ) -> dict:
        return await nats_rpc.invoke(tenant, device_id, function, params, timeout)

    async def rpc_connect(self) -> Any:
        return await nats_rpc.connect()

    async def subscribe_events(
        self, client: Any, subject: str, callback,
    ) -> Any:
        return await client.subscribe(subject, cb=callback)

    async def unsubscribe_events(self, client: Any, subscription: Any) -> None:
        if subscription:
            await subscription.unsubscribe()
        if client:
            await client.close()

    async def run_verification(self) -> list[dict]:
        return await verification.run_verification()

    def broker_display_info(self) -> dict:
        return {
            "backend": "NATS",
            "host": config.NATS_HOST,
            "port": config.NATS_PORT,
            "auth_method": "JWT (nsc)",
            "container": config.NATS_CONTAINER,
        }

    def default_host(self) -> str:
        return config.NATS_HOST

    def default_port(self) -> str:
        return config.NATS_PORT
