"""StrandsDeviceConnectAgent — DeviceConnectAgent with a Strands backend.

Usage::

    from device_connect_agent_tools.adapters.strands_agent import StrandsDeviceConnectAgent

    agent = StrandsDeviceConnectAgent(
        goal="Monitor IoT devices and react to events",
        model_id="claude-sonnet-4-20250514",
    )
    async with agent:
        await agent.run()

Requires: pip install device-connect-agent-tools[strands]
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from device_connect_agent_tools.agent import DeviceConnectAgent

logger = logging.getLogger(__name__)


class StrandsDeviceConnectAgent(DeviceConnectAgent):
    """DeviceConnectAgent that uses Strands Agent for LLM inference."""

    def __init__(
        self,
        goal: str,
        model_id: str = "claude-sonnet-4-20250514",
        max_tokens: int = 4096,
        system_prompt: Optional[str] = None,
        **kwargs: Any,
    ):
        """Initialize with Strands-specific parameters.

        Args:
            goal: The agent's objective.
            model_id: Anthropic model ID for Strands Agent.
            max_tokens: Maximum tokens for LLM responses.
            system_prompt: Custom system prompt. If None, one is built
                from discovered devices during prepare().
            **kwargs: Passed to DeviceConnectAgent (nats_url, batch_window, etc.).
        """
        super().__init__(goal=goal, **kwargs)
        self._model_id = model_id
        self._max_tokens = max_tokens
        self._custom_system_prompt = system_prompt
        self._agent = None

    async def prepare(self) -> Dict[str, Any]:
        """Connect, discover devices, and create the Strands Agent."""
        from strands import Agent
        from strands.models import AnthropicModel
        from device_connect_agent_tools.adapters.strands import (
            describe_fleet,
            list_devices,
            get_device_functions,
            invoke_device,
            invoke_device_with_fallback,
            get_device_status,
        )

        result = await super().prepare()

        system_prompt = self._custom_system_prompt or self._build_system_prompt()
        self._agent = Agent(
            model=AnthropicModel(model_id=self._model_id, max_tokens=self._max_tokens),
            tools=[
                describe_fleet, list_devices, get_device_functions,
                invoke_device, invoke_device_with_fallback, get_device_status,
            ],
            system_prompt=system_prompt,
        )
        return result

    def _run_agent_sync(self, prompt: str) -> str:
        """Invoke the Strands Agent with the given prompt."""
        logger.info("Sending prompt to Strands Agent (%d chars)", len(prompt))
        response = str(self._agent(prompt))
        logger.info("Agent response: %s", response[:200])
        return response

    def _build_system_prompt(self) -> str:
        """Build a system prompt from discovered devices.

        Uses a compact fleet summary instead of dumping all device schemas.
        The agent can use describe_fleet(), list_devices(), and
        get_device_functions() to drill into details as needed.
        """
        # Build compact fleet summary (type counts + locations)
        from collections import defaultdict
        by_type: dict = defaultdict(lambda: {"count": 0, "locations": set()})
        for d in self.devices:
            dt = d.get("device_type") or d.get("identity", {}).get("device_type") or "?"
            loc = d.get("location") or d.get("status", {}).get("location") or "?"
            by_type[dt]["count"] += 1
            by_type[dt]["locations"].add(loc)

        type_lines = []
        for dt, info in sorted(by_type.items()):
            locs = ", ".join(sorted(info["locations"]))
            type_lines.append(f"  - {info['count']}x {dt} (at: {locs})")
        fleet_summary = "\n".join(type_lines) or "  (none yet — call describe_fleet() to refresh)"

        return (
            f"You are an AI agent connected to the Device Connect IoT network.\n\n"
            f"YOUR GOAL: {self.goal}\n\n"
            f"FLEET OVERVIEW ({len(self.devices)} devices):\n{fleet_summary}\n\n"
            f"DISCOVERY TOOLS:\n"
            f"  - describe_fleet() — fleet summary (what you see above)\n"
            f"  - list_devices(device_type=..., location=...) — browse devices\n"
            f"  - get_device_functions(device_id) — see what a device can do\n"
            f"  - invoke_device(device_id, function, params) — call a device function\n\n"
            f"INSTRUCTIONS:\n"
            f"When you receive device events, you MUST:\n"
            f"1. Analyze the events\n"
            f"2. Use get_device_functions() to check available functions if needed\n"
            f"3. Use invoke_device() to interact with devices\n"
            f"4. Report what you found and what actions you took\n\n"
            f"Always provide llm_reasoning when invoking devices to explain your decision.\n"
            f"Always call at least one tool per batch of events."
        )
