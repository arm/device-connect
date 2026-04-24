# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for Strands Agent with device-connect-agent-tools.

Tests that a Strands Agent can use Device Connect tools to discover and invoke devices.
Requires LLM API key.
"""

import asyncio
import os

import pytest


SETTLE_TIME = 0.5

# Pinned model IDs for reproducible CI results.
ANTHROPIC_MODEL_ID = "claude-sonnet-4-20250514"
OPENAI_MODEL_ID = "gpt-4o"


def _get_api_key():
    """Get LLM API key from env."""
    for env_var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        key = os.getenv(env_var)
        if key:
            return key.strip(), env_var
    return None, None


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.llm
async def test_strands_agent_discovers_devices(device_spawner, messaging_url):
    """Strands Agent should use discover_devices() tool to find registered devices."""
    api_key, env_var = _get_api_key()
    if not api_key:
        pytest.skip("No LLM API key found")

    await device_spawner.spawn_camera("itest-strands-cam")
    await device_spawner.spawn_sensor("itest-strands-sensor")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, discover_devices

    connect(nats_url=messaging_url)
    try:
        from strands import Agent

        if "ANTHROPIC" in env_var:
            from strands.models import AnthropicModel
            model = AnthropicModel(model_id=ANTHROPIC_MODEL_ID, max_tokens=4096)
        else:
            from strands.models import OpenAIModel
            model = OpenAIModel(model_id=OPENAI_MODEL_ID)

        agent = Agent(
            model=model,
            tools=[discover_devices],
            system_prompt="You discover IoT devices. Call discover_devices() when asked.",
        )
        response = agent("What devices are available? Use discover_devices to find out.")
        assert response is not None
        assert len(str(response)) > 0
    finally:
        disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.llm
async def test_strands_agent_invokes_device(device_spawner, messaging_url):
    """Strands Agent should use invoke_device() to call sensor get_reading."""
    api_key, env_var = _get_api_key()
    if not api_key:
        pytest.skip("No LLM API key found")

    await device_spawner.spawn_sensor(
        "itest-strands-invoke-sensor", initial_temp=21.0, initial_humidity=40.0,
    )
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, discover_devices, invoke_device

    connect(nats_url=messaging_url)
    try:
        from strands import Agent

        if "ANTHROPIC" in env_var:
            from strands.models import AnthropicModel
            model = AnthropicModel(model_id=ANTHROPIC_MODEL_ID, max_tokens=4096)
        else:
            from strands.models import OpenAIModel
            model = OpenAIModel(model_id=OPENAI_MODEL_ID)

        agent = Agent(
            model=model,
            tools=[discover_devices, invoke_device],
            system_prompt=(
                "You interact with IoT devices. First discover devices, "
                "then invoke the sensor's get_reading function."
            ),
        )
        response = agent(
            "Find all devices, then read the temperature from the sensor. "
            "Use discover_devices first, then invoke_device on the sensor."
        )
        assert response is not None
    finally:
        disconnect()
