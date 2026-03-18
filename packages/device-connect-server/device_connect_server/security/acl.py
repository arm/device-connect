"""
Access Control List (ACL) models and utilities for Device Connect.

ACLs control which devices can:
- See other devices (discovery visibility)
- Subscribe to events from other devices
- Send commands to other devices
"""

from __future__ import annotations

import fnmatch
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class EventACL(BaseModel):
    """Access control for a specific event type."""

    event_name: str = Field(description="Event name (e.g., 'event/objectDetected')")
    allowed_subscribers: List[str] = Field(
        default_factory=list,
        description="List of device IDs or patterns that can subscribe to this event"
    )
    denied_subscribers: List[str] = Field(
        default_factory=list,
        description="List of device IDs or patterns explicitly denied"
    )


class FunctionACL(BaseModel):
    """Access control for a specific function."""

    function_name: str = Field(description="Function name (e.g., 'dispatchRobot')")
    allowed_callers: List[str] = Field(
        default_factory=list,
        description="List of device IDs or patterns that can call this function"
    )
    denied_callers: List[str] = Field(
        default_factory=list,
        description="List of device IDs or patterns explicitly denied"
    )
    require_approval: bool = Field(
        default=False,
        description="If true, function calls require human approval"
    )


class DeviceACL(BaseModel):
    """Complete ACL configuration for a device."""

    device_id: str = Field(description="Device ID this ACL applies to")
    tenant: str = Field(default="default", description="Tenant namespace")

    # Discovery visibility
    visible_to: List[str] = Field(
        default_factory=lambda: ["*"],
        description="List of device IDs or patterns that can see this device in discovery"
    )
    hidden_from: List[str] = Field(
        default_factory=list,
        description="List of device IDs or patterns that cannot see this device"
    )

    # Event subscriptions
    event_acls: List[EventACL] = Field(
        default_factory=list,
        description="Per-event access control rules"
    )
    default_event_subscribers: List[str] = Field(
        default_factory=lambda: ["orchestrator-*"],
        description="Default allowed subscribers for events not explicitly listed"
    )

    # Function calls
    function_acls: List[FunctionACL] = Field(
        default_factory=list,
        description="Per-function access control rules"
    )
    default_function_callers: List[str] = Field(
        default_factory=lambda: ["orchestrator-*"],
        description="Default allowed callers for functions not explicitly listed"
    )

    # Global deny list (takes precedence over all allows)
    global_deny_list: List[str] = Field(
        default_factory=list,
        description="Device IDs or patterns globally denied from any interaction"
    )

    # Metadata
    description: Optional[str] = Field(
        None,
        description="Human-readable description of ACL policy"
    )
    tags: List[str] = Field(
        default_factory=list,
        description="Tags for grouping and managing ACLs"
    )


class ACLMatcher:
    """Helper class for matching device IDs against ACL patterns."""

    @staticmethod
    def matches(device_id: str, patterns: List[str]) -> bool:
        """
        Check if device_id matches any of the patterns.

        Patterns support wildcards:
        - * matches any sequence of characters
        - ? matches any single character
        - [abc] matches any character in the set
        - [!abc] matches any character not in the set

        Examples:
        - "camera-*" matches "camera-001", "camera-lobby"
        - "robot-?" matches "robot-1", "robot-a"
        - "orchestrator-*" matches all orchestrators
        - "*" matches everything
        """
        if not patterns:
            return False

        for pattern in patterns:
            if fnmatch.fnmatch(device_id, pattern):
                return True

        return False

    @staticmethod
    def can_see_device(
        requester_id: str,
        target_acl: DeviceACL
    ) -> bool:
        """Check if requester can see target device in discovery."""

        # Global deny takes precedence
        if ACLMatcher.matches(requester_id, target_acl.global_deny_list):
            return False

        # Check explicit deny
        if ACLMatcher.matches(requester_id, target_acl.hidden_from):
            return False

        # Check explicit allow
        return ACLMatcher.matches(requester_id, target_acl.visible_to)

    @staticmethod
    def can_subscribe_to_event(
        subscriber_id: str,
        target_acl: DeviceACL,
        event_name: str
    ) -> bool:
        """Check if subscriber can subscribe to target device's event."""

        # Global deny takes precedence
        if ACLMatcher.matches(subscriber_id, target_acl.global_deny_list):
            return False

        # Check event-specific ACL
        for event_acl in target_acl.event_acls:
            if event_acl.event_name == event_name:
                # Explicit deny
                if ACLMatcher.matches(subscriber_id, event_acl.denied_subscribers):
                    return False
                # Explicit allow
                return ACLMatcher.matches(subscriber_id, event_acl.allowed_subscribers)

        # Fall back to default
        return ACLMatcher.matches(subscriber_id, target_acl.default_event_subscribers)

    @staticmethod
    def can_call_function(
        caller_id: str,
        target_acl: DeviceACL,
        function_name: str
    ) -> tuple[bool, bool]:
        """
        Check if caller can invoke target device's function.

        Returns:
            (allowed, requires_approval)
        """

        # Global deny takes precedence
        if ACLMatcher.matches(caller_id, target_acl.global_deny_list):
            return False, False

        # Check function-specific ACL
        for func_acl in target_acl.function_acls:
            if func_acl.function_name == function_name:
                # Explicit deny
                if ACLMatcher.matches(caller_id, func_acl.denied_callers):
                    return False, False
                # Explicit allow
                allowed = ACLMatcher.matches(caller_id, func_acl.allowed_callers)
                return allowed, func_acl.require_approval

        # Fall back to default
        allowed = ACLMatcher.matches(caller_id, target_acl.default_function_callers)
        return allowed, False


class ACLManager:
    """Manages ACLs for all devices in Device Connect."""

    def __init__(self):
        self._acls: Dict[str, DeviceACL] = {}

    def set_acl(self, acl: DeviceACL) -> None:
        """Store or update ACL for a device."""
        key = f"{acl.tenant}:{acl.device_id}"
        self._acls[key] = acl

    def get_acl(self, device_id: str, tenant: str = "default") -> Optional[DeviceACL]:
        """Retrieve ACL for a device."""
        key = f"{tenant}:{device_id}"
        return self._acls.get(key)

    def delete_acl(self, device_id: str, tenant: str = "default") -> None:
        """Remove ACL for a device."""
        key = f"{tenant}:{device_id}"
        self._acls.pop(key, None)

    def filter_visible_devices(
        self,
        requester_id: str,
        devices: List[Dict],
        tenant: str = "default"
    ) -> List[Dict]:
        """
        Filter device list to only include devices visible to requester.

        Args:
            requester_id: ID of device requesting device list
            devices: List of device info dicts
            tenant: Tenant namespace

        Returns:
            Filtered list of devices
        """
        visible = []

        for device in devices:
            device_id = device.get("device_id")
            if not device_id:
                continue

            # Get ACL for this device (or use default permissive policy)
            acl = self.get_acl(device_id, tenant)
            if acl is None:
                # No ACL = visible to all (default permissive)
                acl = DeviceACL(device_id=device_id, tenant=tenant)

            # Check visibility
            if ACLMatcher.can_see_device(requester_id, acl):
                visible.append(device)

        return visible

    def check_event_subscription(
        self,
        subscriber_id: str,
        target_device_id: str,
        event_name: str,
        tenant: str = "default"
    ) -> bool:
        """Check if subscriber can subscribe to target device's event."""

        acl = self.get_acl(target_device_id, tenant)
        if acl is None:
            # No ACL = allow (default permissive)
            acl = DeviceACL(device_id=target_device_id, tenant=tenant)

        return ACLMatcher.can_subscribe_to_event(subscriber_id, acl, event_name)

    def check_function_call(
        self,
        caller_id: str,
        target_device_id: str,
        function_name: str,
        tenant: str = "default"
    ) -> tuple[bool, bool]:
        """
        Check if caller can invoke target device's function.

        Returns:
            (allowed, requires_approval)
        """

        acl = self.get_acl(target_device_id, tenant)
        if acl is None:
            # No ACL = allow (default permissive)
            acl = DeviceACL(device_id=target_device_id, tenant=tenant)

        return ACLMatcher.can_call_function(caller_id, acl, function_name)

    def list_acls(self, tenant: Optional[str] = None) -> List[DeviceACL]:
        """List all ACLs, optionally filtered by tenant."""
        if tenant:
            return [acl for acl in self._acls.values() if acl.tenant == tenant]
        return list(self._acls.values())
