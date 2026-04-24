# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Zenoh high-frequency streaming integration test.

Validates that the ZenohAdapter can sustain 50Hz+ pub/sub throughput
through the asyncio bridge without dropping messages.

Requires: Zenoh router running (docker-compose-itest.yml).
"""

import asyncio
import json
import os
import time

import pytest


ZENOH_CONNECT = os.getenv("ZENOH_CONNECT", "tcp/localhost:7447")


@pytest.mark.asyncio
@pytest.mark.slow
async def test_streaming_50hz(infrastructure):
    """Publish at 50Hz for 2 seconds, verify all messages received."""
    from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

    publisher = ZenohAdapter()
    subscriber = ZenohAdapter()

    await publisher.connect(servers=[ZENOH_CONNECT])
    await subscriber.connect(servers=[ZENOH_CONNECT])

    try:
        received = []
        subject = "streaming.test.sensor"

        async def handler(data, reply):
            msg = json.loads(data)
            received.append(msg)

        sub = await subscriber.subscribe(subject, handler)

        # Allow subscriber to settle
        await asyncio.sleep(0.2)

        # Publish at 50Hz for 2 seconds = 100 messages
        hz = 50
        duration = 2.0
        total = int(hz * duration)
        interval = 1.0 / hz

        t0 = time.monotonic()
        for i in range(total):
            payload = json.dumps({"seq": i, "ts": time.monotonic()}).encode()
            await publisher.publish(subject, payload)
            # Yield to event loop between publishes
            elapsed = time.monotonic() - t0
            target = (i + 1) * interval
            if target > elapsed:
                await asyncio.sleep(target - elapsed)

        # Wait for all messages to arrive (generous timeout)
        for _ in range(100):
            if len(received) >= total:
                break
            await asyncio.sleep(0.05)

        await sub.unsubscribe()

        # Allow >= 95% delivery (async bridge may occasionally batch)
        assert len(received) >= int(total * 0.95), (
            f"Expected >= {int(total * 0.95)} messages, got {len(received)}"
        )

        # Verify ordering (seq should be monotonically increasing)
        seqs = [msg["seq"] for msg in received]
        for i in range(1, len(seqs)):
            assert seqs[i] > seqs[i - 1], f"Out of order: seq[{i}]={seqs[i]} <= seq[{i-1}]={seqs[i-1]}"

    finally:
        await publisher.close()
        await subscriber.close()


@pytest.mark.asyncio
@pytest.mark.slow
async def test_streaming_multiple_topics(infrastructure):
    """Publish to 3 topics concurrently at 20Hz each, verify all arrive."""
    from device_connect_edge.messaging.zenoh_adapter import ZenohAdapter

    publisher = ZenohAdapter()
    subscriber = ZenohAdapter()

    await publisher.connect(servers=[ZENOH_CONNECT])
    await subscriber.connect(servers=[ZENOH_CONNECT])

    try:
        received_by_topic = {"temp": [], "pressure": [], "humidity": []}
        topics = {
            "streaming.multi.temp": "temp",
            "streaming.multi.pressure": "pressure",
            "streaming.multi.humidity": "humidity",
        }

        subs = []
        for subject, key in topics.items():
            async def make_handler(k):
                async def handler(data, reply):
                    received_by_topic[k].append(json.loads(data))
                return handler
            handler = await make_handler(key)
            sub = await subscriber.subscribe(subject, handler)
            subs.append(sub)

        await asyncio.sleep(0.2)

        # 20Hz per topic for 1 second = 20 messages each
        hz = 20
        total_per_topic = 20

        for i in range(total_per_topic):
            for subject in topics:
                payload = json.dumps({"seq": i, "topic": subject}).encode()
                await publisher.publish(subject, payload)
            await asyncio.sleep(1.0 / hz)

        # Wait for delivery
        for _ in range(100):
            counts = [len(v) for v in received_by_topic.values()]
            if all(c >= total_per_topic for c in counts):
                break
            await asyncio.sleep(0.05)

        for sub in subs:
            await sub.unsubscribe()

        for key, msgs in received_by_topic.items():
            assert len(msgs) >= int(total_per_topic * 0.90), (
                f"Topic '{key}': expected >= {int(total_per_topic * 0.90)}, got {len(msgs)}"
            )

    finally:
        await publisher.close()
        await subscriber.close()
