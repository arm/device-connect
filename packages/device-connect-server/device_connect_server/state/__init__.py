# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""State management for Device Connect.

This module provides the StateStore ABC for key-value state storage
with TTL and distributed locks, plus concrete implementations.

Available implementations:
    - EtcdStateStore: Production-ready backend using etcd3

Example:
    from device_connect_server.state import EtcdStateStore

    store = EtcdStateStore(host="localhost", port=2379)
    await store.connect()
    await store.set("experiments/EXP-001", {"status": "running"})
    data = await store.get("experiments/EXP-001")
    await store.close()
"""
from device_connect_server.state.base import StateStore
from device_connect_server.state.etcd_store import EtcdStateStore

__all__ = [
    "StateStore",
    "EtcdStateStore",
]
