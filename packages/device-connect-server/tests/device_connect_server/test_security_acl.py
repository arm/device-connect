"""Tests for device_connect_server.security.acl module."""

import pytest
from device_connect_server.security.acl import (
    EventACL,
    FunctionACL,
    DeviceACL,
    ACLMatcher,
    ACLManager,
)


# ── Pydantic model tests ──────────────────────────────────────────


class TestEventACL:
    def test_create(self):
        acl = EventACL(event_name="objectDetected", allowed_subscribers=["cam-*"])
        assert acl.event_name == "objectDetected"
        assert acl.allowed_subscribers == ["cam-*"]
        assert acl.denied_subscribers == []

    def test_serialization(self):
        acl = EventACL(event_name="alert", denied_subscribers=["rogue-001"])
        d = acl.model_dump()
        assert d["event_name"] == "alert"
        assert d["denied_subscribers"] == ["rogue-001"]


class TestFunctionACL:
    def test_create(self):
        acl = FunctionACL(
            function_name="dispatchRobot",
            allowed_callers=["orchestrator-*"],
            require_approval=True,
        )
        assert acl.function_name == "dispatchRobot"
        assert acl.require_approval is True

    def test_defaults(self):
        acl = FunctionACL(function_name="ping")
        assert acl.allowed_callers == []
        assert acl.denied_callers == []
        assert acl.require_approval is False


class TestDeviceACL:
    def test_defaults(self):
        acl = DeviceACL(device_id="cam-001")
        assert acl.tenant == "default"
        assert acl.visible_to == ["*"]
        assert acl.hidden_from == []
        assert acl.default_event_subscribers == ["orchestrator-*"]
        assert acl.default_function_callers == ["orchestrator-*"]
        assert acl.global_deny_list == []

    def test_custom(self):
        acl = DeviceACL(
            device_id="cam-001",
            tenant="lab",
            visible_to=["orchestrator-*", "dashboard-*"],
            hidden_from=["rogue-*"],
            global_deny_list=["blacklisted-001"],
        )
        assert acl.tenant == "lab"
        assert "rogue-*" in acl.hidden_from


# ── ACLMatcher tests ──────────────────────────────────────────────


class TestACLMatcher:
    def test_matches_wildcard(self):
        assert ACLMatcher.matches("camera-001", ["camera-*"]) is True
        assert ACLMatcher.matches("robot-001", ["camera-*"]) is False

    def test_matches_star(self):
        assert ACLMatcher.matches("anything", ["*"]) is True

    def test_matches_empty_patterns(self):
        assert ACLMatcher.matches("camera-001", []) is False

    def test_matches_exact(self):
        assert ACLMatcher.matches("cam-001", ["cam-001"]) is True
        assert ACLMatcher.matches("cam-002", ["cam-001"]) is False

    def test_matches_question_mark(self):
        assert ACLMatcher.matches("robot-1", ["robot-?"]) is True
        assert ACLMatcher.matches("robot-12", ["robot-?"]) is False

    def test_can_see_device_default_visible(self):
        acl = DeviceACL(device_id="cam-001")
        assert ACLMatcher.can_see_device("anyone", acl) is True

    def test_can_see_device_hidden(self):
        acl = DeviceACL(device_id="cam-001", hidden_from=["rogue-*"])
        assert ACLMatcher.can_see_device("rogue-001", acl) is False
        assert ACLMatcher.can_see_device("orchestrator-main", acl) is True

    def test_can_see_device_global_deny(self):
        acl = DeviceACL(device_id="cam-001", global_deny_list=["bad-*"])
        assert ACLMatcher.can_see_device("bad-actor", acl) is False

    def test_can_subscribe_default(self):
        acl = DeviceACL(device_id="cam-001")
        assert ACLMatcher.can_subscribe_to_event("orchestrator-main", acl, "alert") is True
        assert ACLMatcher.can_subscribe_to_event("random-device", acl, "alert") is False

    def test_can_subscribe_explicit_event_acl(self):
        acl = DeviceACL(
            device_id="cam-001",
            event_acls=[
                EventACL(event_name="alert", allowed_subscribers=["dashboard-*"]),
            ],
        )
        assert ACLMatcher.can_subscribe_to_event("dashboard-1", acl, "alert") is True
        assert ACLMatcher.can_subscribe_to_event("orchestrator-main", acl, "alert") is False
        # Other events fall back to default
        assert ACLMatcher.can_subscribe_to_event("orchestrator-main", acl, "other") is True

    def test_can_subscribe_denied(self):
        acl = DeviceACL(
            device_id="cam-001",
            event_acls=[
                EventACL(
                    event_name="alert",
                    allowed_subscribers=["*"],
                    denied_subscribers=["rogue-*"],
                ),
            ],
        )
        assert ACLMatcher.can_subscribe_to_event("rogue-001", acl, "alert") is False

    def test_can_call_function_default(self):
        acl = DeviceACL(device_id="robot-001")
        allowed, approval = ACLMatcher.can_call_function("orchestrator-main", acl, "dispatch")
        assert allowed is True
        assert approval is False

    def test_can_call_function_denied(self):
        acl = DeviceACL(device_id="robot-001", global_deny_list=["bad-*"])
        allowed, _ = ACLMatcher.can_call_function("bad-actor", acl, "dispatch")
        assert allowed is False

    def test_can_call_function_requires_approval(self):
        acl = DeviceACL(
            device_id="robot-001",
            function_acls=[
                FunctionACL(
                    function_name="selfDestruct",
                    allowed_callers=["orchestrator-*"],
                    require_approval=True,
                ),
            ],
        )
        allowed, approval = ACLMatcher.can_call_function("orchestrator-main", acl, "selfDestruct")
        assert allowed is True
        assert approval is True

    def test_can_call_function_specific_deny(self):
        acl = DeviceACL(
            device_id="robot-001",
            function_acls=[
                FunctionACL(
                    function_name="dispatch",
                    allowed_callers=["*"],
                    denied_callers=["intern-*"],
                ),
            ],
        )
        allowed, _ = ACLMatcher.can_call_function("intern-001", acl, "dispatch")
        assert allowed is False


# ── ACLManager tests ──────────────────────────────────────────────


class TestACLManager:
    def test_set_and_get(self):
        mgr = ACLManager()
        acl = DeviceACL(device_id="cam-001")
        mgr.set_acl(acl)
        assert mgr.get_acl("cam-001") is acl

    def test_get_missing(self):
        mgr = ACLManager()
        assert mgr.get_acl("nonexistent") is None

    def test_delete(self):
        mgr = ACLManager()
        mgr.set_acl(DeviceACL(device_id="cam-001"))
        mgr.delete_acl("cam-001")
        assert mgr.get_acl("cam-001") is None

    def test_delete_missing_no_error(self):
        mgr = ACLManager()
        mgr.delete_acl("nonexistent")  # should not raise

    def test_filter_visible_devices(self):
        mgr = ACLManager()
        mgr.set_acl(DeviceACL(device_id="cam-001", hidden_from=["rogue-*"]))
        devices = [
            {"device_id": "cam-001"},
            {"device_id": "robot-001"},
        ]
        visible = mgr.filter_visible_devices("rogue-001", devices)
        ids = [d["device_id"] for d in visible]
        assert "cam-001" not in ids
        assert "robot-001" in ids  # no ACL = default permissive

    def test_check_event_subscription_no_acl_permissive(self):
        mgr = ACLManager()
        # No ACL set → default DeviceACL → default_event_subscribers = ["orchestrator-*"]
        assert mgr.check_event_subscription("orchestrator-main", "cam-001", "alert") is True
        assert mgr.check_event_subscription("random", "cam-001", "alert") is False

    def test_check_function_call_no_acl(self):
        mgr = ACLManager()
        allowed, approval = mgr.check_function_call("orchestrator-main", "robot-001", "dispatch")
        assert allowed is True
        assert approval is False

    def test_list_acls(self):
        mgr = ACLManager()
        mgr.set_acl(DeviceACL(device_id="a", tenant="lab"))
        mgr.set_acl(DeviceACL(device_id="b", tenant="prod"))
        assert len(mgr.list_acls()) == 2
        assert len(mgr.list_acls(tenant="lab")) == 1

    def test_tenant_isolation(self):
        mgr = ACLManager()
        mgr.set_acl(DeviceACL(device_id="cam-001", tenant="lab"))
        assert mgr.get_acl("cam-001", tenant="lab") is not None
        assert mgr.get_acl("cam-001", tenant="prod") is None
