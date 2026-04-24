# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Orchestrator fixtures for cross-repo integration tests.

- MockOrchestrator: Rule-based routing (no LLM, fast)
- RealOrchestratorLite: litellm-based LLM routing (~8s startup)
- RealOrchestratorStrands: Strands-based LLM routing (~25s startup)
"""

import asyncio
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from device_connect_edge.messaging import create_client

logger = logging.getLogger(__name__)

# Path to core/ sibling directory (orchestrator subprocess runs fabric.orchestration from there)
CORE_ROOT = Path(__file__).resolve().parents[2] / "core"


@dataclass
class RoutingRule:
    on_event: str
    call_function: str
    target_device_type: Optional[str] = None
    target_device_id: Optional[str] = None
    condition: Optional[Callable[[dict], bool]] = None
    transform_params: Optional[Callable[[dict], dict]] = None

    def matches(self, event_name: str, event_data: dict) -> bool:
        if self.on_event != event_name:
            return False
        if self.condition and not self.condition(event_data):
            return False
        return True

    def get_params(self, event_data: dict) -> dict:
        if self.transform_params:
            return self.transform_params(event_data)
        return {"zone_id": event_data.get("zone_id", "unknown")}


class MockOrchestrator:
    """Rule-based orchestrator (no LLM) for fast integration tests.

    Supports NATS and Zenoh backends via the SDK MessagingClient abstraction.
    """

    def __init__(self, backend: str, url: str, tenant: str = "default"):
        self.backend = backend
        self.url = url
        self.tenant = tenant
        self._messaging = None
        self._rules: List[RoutingRule] = []
        self._subscriptions: list = []

    async def __aenter__(self) -> "MockOrchestrator":
        await self.start()
        return self

    async def __aexit__(self, *args) -> None:
        await self.stop()

    def add_rule(self, on_event: str, call_function: str, **kwargs) -> "MockOrchestrator":
        self._rules.append(RoutingRule(on_event=on_event, call_function=call_function, **kwargs))
        return self

    def clear_rules(self) -> None:
        self._rules.clear()

    async def start(self) -> None:
        self._messaging = create_client(self.backend)
        await self._messaging.connect(servers=[self.url])
        subject = f"device-connect.{self.tenant}.*.event.>"
        sub = await self._messaging.subscribe_with_subject(subject, self._handle_event)
        self._subscriptions.append(sub)
        logger.info(f"MockOrchestrator started, subscribed to: {subject}")

    async def stop(self) -> None:
        for sub in self._subscriptions:
            await sub.unsubscribe()
        self._subscriptions.clear()
        if self._messaging:
            await self._messaging.close()

    async def _handle_event(self, data: bytes, subject: str, reply: Optional[str]) -> None:
        try:
            # Normalize Zenoh slash-separated subjects to dot-separated
            normalized = subject.replace("/", ".")
            payload = json.loads(data)
            event_data = payload.get("params", payload)
            parts = normalized.split(".")
            if len(parts) < 5:
                return
            event_name = ".".join(parts[4:])
            source_device = parts[2]
            for rule in self._rules:
                if rule.matches(event_name, event_data):
                    await self._execute_rule(rule, event_data, source_device)
        except Exception as e:
            logger.error(f"Error handling event: {e}")

    async def _execute_rule(self, rule: RoutingRule, event_data: dict, source_device: str) -> None:
        target = await self._find_target(rule)
        if not target:
            logger.warning(f"No target device for rule: {rule.on_event}")
            return
        params = rule.get_params(event_data)
        await self._call_function(target, rule.call_function, params)

    async def _find_target(self, rule: RoutingRule) -> Optional[str]:
        if rule.target_device_id:
            return rule.target_device_id
        if rule.target_device_type:
            devices = await self._query_devices()
            for did, info in devices.items():
                dt = info.get("identity", {}).get("device_type")
                if dt == rule.target_device_type:
                    return did
        return None

    async def _query_devices(self) -> Dict[str, dict]:
        try:
            request = {"jsonrpc": "2.0", "id": "mock-query", "method": "discovery/listDevices", "params": {}}
            response = await self._messaging.request(
                f"device-connect.{self.tenant}.discovery", json.dumps(request).encode(), timeout=5.0,
            )
            data = json.loads(response)
            if "result" in data:
                return {d["device_id"]: d for d in data["result"].get("devices", [])}
        except Exception as e:
            logger.error(f"Error querying devices: {e}")
        return {}

    async def _call_function(self, device_id: str, function_name: str, params: dict) -> Optional[dict]:
        try:
            request = {"jsonrpc": "2.0", "id": f"mock-{device_id}-{function_name}", "method": function_name, "params": params}
            logger.info(f"Calling {device_id}::{function_name}({params})")
            response = await self._messaging.request(
                f"device-connect.{self.tenant}.{device_id}.cmd", json.dumps(request).encode(), timeout=30.0,
            )
            return json.loads(response)
        except Exception as e:
            logger.error(f"Error calling function: {e}")
            return None


class RealOrchestratorLite:
    """litellm-based orchestrator subprocess (requires API key, ~8s startup)."""

    def __init__(self, nats_url: str, api_key: str, tenant: str = "default"):
        self.nats_url = nats_url
        self.api_key = api_key
        self.tenant = tenant
        self._process: Optional[subprocess.Popen] = None

    async def __aenter__(self) -> "RealOrchestratorLite":
        await self.start()
        return self

    async def __aexit__(self, *args) -> None:
        await self.stop()

    async def start(self) -> None:
        env = os.environ.copy()
        env.update({
            "NATS_URL": self.nats_url,
            "OPENAI_API_KEY": self.api_key,
            "TENANT": self.tenant,
            "DEVICE_CONNECT_ALLOW_INSECURE": "true",
            "DEVICE_ID": "itest-orchestrator-lite",
            "PYTHONPATH": str(CORE_ROOT),
        })
        self._process = subprocess.Popen(
            [
                "python", "-u", "-m", "fabric.orchestration",
                "--goal", "Process events and coordinate devices for integration testing.",
                "--provider", "openai",
                "--subscribe-all",
                "--lite",
            ],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=CORE_ROOT,
        )
        await asyncio.sleep(8)
        if self._process.poll() is not None:
            stdout, _ = self._process.communicate()
            raise RuntimeError(f"Orchestrator (lite) failed: {stdout.decode() if stdout else 'Unknown'}")
        logger.info("RealOrchestratorLite started")

    async def stop(self) -> None:
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()


class RealOrchestratorStrands:
    """Strands-based orchestrator subprocess (requires API key, ~25s startup)."""

    def __init__(self, nats_url: str, api_key: str, tenant: str = "default"):
        self.nats_url = nats_url
        self.api_key = api_key
        self.tenant = tenant
        self._process: Optional[subprocess.Popen] = None

    async def __aenter__(self) -> "RealOrchestratorStrands":
        await self.start()
        return self

    async def __aexit__(self, *args) -> None:
        await self.stop()

    async def start(self) -> None:
        env = os.environ.copy()
        env.update({
            "NATS_URL": self.nats_url,
            "OPENAI_API_KEY": self.api_key,
            "TENANT": self.tenant,
            "DEVICE_CONNECT_ALLOW_INSECURE": "true",
            "DEVICE_ID": "itest-orchestrator-strands",
            "PYTHONPATH": str(CORE_ROOT),
        })
        self._process = subprocess.Popen(
            [
                "python", "-u", "-m", "fabric.orchestration",
                "--goal", "Process events and coordinate devices for integration testing.",
                "--provider", "openai",
                "--subscribe-all",
            ],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=CORE_ROOT,
        )
        await asyncio.sleep(25)
        if self._process.poll() is not None:
            stdout, _ = self._process.communicate()
            raise RuntimeError(f"Orchestrator (strands) failed: {stdout.decode() if stdout else 'Unknown'}")
        logger.info("RealOrchestratorStrands started")

    async def stop(self) -> None:
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
