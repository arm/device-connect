"""Shared memory IPC for zero-copy data transfer between containers.

Provides high-level abstractions over Zenoh SHM and optional iceoryx2
for transferring high-bandwidth data (camera frames, LiDAR point clouds)
between co-located capability containers without copying.
"""

from device_connect_container.shm.provider import ShmDataProvider
from device_connect_container.shm.consumer import ShmDataConsumer
from device_connect_container.shm.channel import ShmChannel

__all__ = [
    "ShmDataProvider",
    "ShmDataConsumer",
    "ShmChannel",
]
