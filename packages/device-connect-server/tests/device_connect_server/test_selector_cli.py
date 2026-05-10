# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Smoke tests for the selector-driven CLI verbs.

Argument-parser shape only; the underlying tools (``discover``,
``invoke``, ``broadcast``, etc.) have their own unit and integration
tests. These guards catch parser-config regressions (missing positional,
typoed dest, alias drift).
"""
from __future__ import annotations

import json

import pytest

from device_connect_server.devctl import cli as devctl_cli
from device_connect_server.devctl import selector_cli
from device_connect_server.statectl import cli as statectl_cli
from device_connect_server.statectl import operations_cli


# -- devctl ---------------------------------------------------------


class TestDevctlSelectorParser:
    def test_discover_requires_selector(self):
        parser = devctl_cli.create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["discover"])

    def test_discover_parses_selector(self):
        parser = devctl_cli.create_parser()
        args = parser.parse_args(["discover", "device(category:camera)"])
        assert args.cmd == "discover"
        assert args.selector == "device(category:camera)"
        assert args.offset == 0
        assert args.limit == 200

    def test_discover_offset_limit_override(self):
        parser = devctl_cli.create_parser()
        args = parser.parse_args(
            ["discover", "device(*)", "--offset", "100", "--limit", "50"]
        )
        assert args.offset == 100
        assert args.limit == 50

    def test_discover_labels_no_key(self):
        parser = devctl_cli.create_parser()
        args = parser.parse_args(["discover-labels"])
        assert args.cmd == "discover-labels"
        assert args.key is None
        assert args.limit == 50

    def test_discover_labels_key_pagination(self):
        parser = devctl_cli.create_parser()
        args = parser.parse_args(
            ["discover-labels", "--key", "device.location", "--limit", "20"]
        )
        assert args.key == "device.location"
        assert args.limit == 20

    def test_legacy_discover_renamed_to_mdns_scan(self):
        # The historical "discover" verb (mDNS scan) now lives under
        # mdns-scan; the alias "scan" keeps it discoverable.
        parser = devctl_cli.create_parser()
        for verb in ("mdns-scan", "scan"):
            args = parser.parse_args([verb])
            # Both aliases share the same args.cmd
            assert args.cmd in ("mdns-scan", "scan")


# -- statectl -------------------------------------------------------


class TestStatectlOperationsParser:
    def test_invoke_requires_selector(self):
        parser = statectl_cli.create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["invoke"])

    def test_invoke_parses(self):
        parser = statectl_cli.create_parser()
        args = parser.parse_args(
            [
                "invoke", "device(robot-001).function(grip_close)",
                "--param", "force_n=10",
                "--reason", "test",
            ]
        )
        assert args.cmd == "invoke"
        assert args.selector == "device(robot-001).function(grip_close)"
        assert args.param == ["force_n=10"]
        assert args.reason == "test"

    def test_invoke_many_with_timeout(self):
        parser = statectl_cli.create_parser()
        args = parser.parse_args(
            [
                "invoke-many",
                "function(safety:critical)",
                "--timeout", "5",
                "--max-concurrency", "8",
            ]
        )
        assert args.cmd == "invoke-many"
        assert float(args.timeout) == 5.0
        assert int(args.max_concurrency) == 8

    def test_broadcast_full_signature(self):
        parser = statectl_cli.create_parser()
        args = parser.parse_args(
            [
                "broadcast",
                "device(category:phone).function(set_flashlight)",
                "--param", "on=true",
                "--param", "color=white",
                "--where", "labels.location == 'lab-A'",
                "--bindings", '{"mask": [[0,1],[1,0]]}',
                "--fire-at", "1700000000.0",
                "--on-late", "fire",
            ]
        )
        assert args.cmd == "broadcast"
        assert args.selector.startswith("device(category:phone)")
        assert args.where == "labels.location == 'lab-A'"
        assert args.on_late == "fire"

    def test_broadcast_rejects_unknown_on_late(self):
        parser = statectl_cli.create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "broadcast", "device(*).function(do)",
                    "--on-late", "bogus",
                ]
            )

    def test_subscribe_parses_correlation_form(self):
        parser = statectl_cli.create_parser()
        args = parser.parse_args(
            ["subscribe", "correlation:br-abc123", "--until", "5"]
        )
        assert args.cmd == "subscribe"
        assert args.selector == "correlation:br-abc123"
        assert int(args.until) == 5

    def test_await_requires_correlation_id(self):
        parser = statectl_cli.create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["await"])

    def test_await_parses(self):
        parser = statectl_cli.create_parser()
        args = parser.parse_args(
            ["await", "br-abc123", "--timeout", "2.5", "--until", "10"]
        )
        assert args.correlation_id == "br-abc123"
        assert float(args.timeout) == 2.5
        assert int(args.until) == 10


# -- parameter parsing ----------------------------------------------


class TestParseParamKV:
    def test_string_values_default(self):
        result = operations_cli._parse_param_kv(["a=hello", "b=world"])
        assert result == {"a": "hello", "b": "world"}

    def test_numbers_decoded(self):
        result = operations_cli._parse_param_kv(["count=5", "ratio=0.75"])
        assert result == {"count": 5, "ratio": 0.75}

    def test_booleans_decoded(self):
        result = operations_cli._parse_param_kv(["on=true", "off=false"])
        assert result == {"on": True, "off": False}

    def test_json_array_decoded(self):
        result = operations_cli._parse_param_kv(["zones=[1,2,3]"])
        assert result == {"zones": [1, 2, 3]}

    def test_json_object_decoded(self):
        result = operations_cli._parse_param_kv(['nested={"a":1}'])
        assert result == {"nested": {"a": 1}}

    def test_string_with_equals(self):
        # The split is on the first '=', so values may contain further '='.
        result = operations_cli._parse_param_kv(["query=a=b"])
        assert result == {"query": "a=b"}

    def test_invalid_form_rejected(self):
        with pytest.raises(ValueError):
            operations_cli._parse_param_kv(["no_equals_sign"])

    def test_empty_key_rejected(self):
        with pytest.raises(ValueError):
            operations_cli._parse_param_kv(["=value"])
