# Discovery and Operations Release Test Summary

Date: 2026-05-21

This document summarizes the test coverage behind the discovery and operations
release. It is written as source material for release notes and public release
documentation.

## Summary

The release has strong automated coverage for selector-based discovery, label
vocabulary, pagination, compact and expanded result shapes, synchronous
invocation, async broadcast, subscriptions, and large simulated fleets. The
largest real integration runs use 200 NATS-backed simulated devices by default.

## Evidence

| Evidence | Scope | Result |
|---|---|---|
| Agent-tools unit tests | `discover`, `discover_labels`, `invoke`, `invoke_many`, `broadcast`, `subscribe`, `await_replies`, error envelopes, truncation, safety warnings | `94 passed in 0.42s` |
| Edge predicate unit tests | Edge-side `where` dependency warning, bounded predicate evaluation, CEL compile/eval behavior | `19 passed in 0.23s` |
| Broadcast/subscription integration tests | Broadcast replies, `where`, bindings, `fire_at`, `on_late`, safety warning, live and snapshot event subscriptions | `12 passed, 12 skipped in 49.53s` on NATS run |
| Large-fleet integration suite | 200-device discovery, pagination, truncation, heterogeneous fleets, ESTOP alias, `invoke_many`, broadcast, `where`, `fire_at`, correlation subscribe | `18 passed in 37.05s` |
| Integration inventory | Hierarchical tools, selector discovery, invoke, broadcast, subscribe, large-fleet tests across NATS/Zenoh where applicable | `144 collected` |

## Coverage Matrix

| Area | What Was Tested | Representative Tests | Confidence |
|---|---|---|---|
| Hierarchical discovery compatibility | `describe_fleet`, `list_devices`, `get_device_functions`, small-fleet expansion, pagination, missing device, status/function counts, deprecation warnings | `test_tools_hierarchical.py`, `packages/.../test_discover.py` | Medium. Compatibility path is covered on small fleets; large-fleet stress is on the selector APIs. |
| Selector discovery | Device, function, and event scopes; bare names; globs; `*`; `key:value`; OR within key; AND across keys; case sensitivity; invalid selectors | `test_tools_selector.py`, `packages/.../test_discover.py` | High. Core selector behavior is covered in unit and integration tests. |
| Labels and truncation | Multi-axis `discover_labels`, per-key pagination, `label_histogram`, `more`, multivalued labels, compact broad rows, expanded drill-down schemas | `test_long_tail_label_histogram_reports_more`, `test_per_key_label_drill_down_bypasses_truncation`, `TestLongTailTruncation` | High for device/function labels; event-label scale remains thinner. |
| Function schemas | Schemas returned for narrow function result sets; broad function queries compact above threshold; drill-down returns `parameters` | `test_large_function_set_stays_compact_and_supports_drill_down`, `test_heterogeneous_discovery_outputs_expected_matrix` | High. Verifies progressive narrowing before returning schemas. |
| Heterogeneous fleets | Sensors, cameras, and robots with different functions/labels; exact category/function histograms; exact `(device_id, function)` matrix | `test_heterogeneous_discovery_outputs_expected_matrix` | High for mixed function discovery. |
| `invoke` | Single-target success, no-match, ambiguous-match, invalid scope, event-scope rejection, JSON-RPC/connection errors, bounded ambiguity preview | `test_tools_invoke.py`, `packages/.../test_invoke.py`, `test_invoke_ambiguity_stays_bounded_in_large_fleet` | High. Exactly-one semantics and common error cases are covered. |
| `invoke_many` | Fan-out success, zero candidates, function-only selector, partial failures, per-target timeout forwarding, concurrency cap, heterogeneous target isolation | `test_invoke_many_partial_failure_accounting_at_scale`, `test_heterogeneous_invoke_many_targets_only_matching_functions` | High. Sync fan-out behavior is covered at small and large scale. |
| ESTOP alias | `function(estop)` discovery and `invoke_many` fan-out target only functions named `estop`, excluding unrelated `safety:critical` decoys | `test_invoke_many_estop_alias_targets_only_estop_functions_at_scale` | Medium-high. API selector/fan-out behavior is covered; this is not a physical safety certification. |
| `broadcast` | Correlation envelope, candidate count, target envelope, zero matches, invalid scope, invalid predicate, publish failure, safety warning, large fan-out replies | `test_broadcast_large_fan_out_returns_correlation_and_target_count`, `test_heterogeneous_broadcast_replies_only_from_matching_functions` | High for async fan-out core. |
| `where` and bindings | CEL predicate filtering, namespaced `bindings`, edge self-election at scale, startup warning for missing predicate support, bounded fail-closed evaluation | `test_broadcast_where_self_election_narrows_large_candidates`, `test_broadcast_where_with_bindings`, `test_device_where.py` | High for current predicate behavior. |
| `fire_at` | Scheduled fan-out, `actually_fired_at` spread, late `on_late=skip` behavior | `test_broadcast_fire_at_synchronizes_large_fan_out`, `test_broadcast_fire_at_late_with_skip_drops` | Medium-high. Large-fleet happy path and small-fleet late/skip behavior are covered. |
| Subscriptions and replies | `subscribe("correlation:<id>")`, live top-level `event(...)`, snapshot `device(...).event(...)`, iterator protocol, race-safe buffer drain, `await_replies` partial/complete collection | `test_subscribe_correlation_drains_large_reply_stream`, `test_subscribe_top_level_event_selector_includes_late_joiners`, `test_subscribe_device_event_selector_is_snapshot`, `test_subscribe.py` | Medium-high. Correlation replies and event subscription semantics are explicitly covered. |

## Large-Scale Testing Approach

Routine PR and CI validation should continue to use 200 real NATS-backed
simulated devices. That size exercises registry discovery, selectors,
pagination, truncation, fan-out, and correlation replies while keeping routine
validation fast and reliable.

10K-device confidence should come from a synthetic registry/load test in a
nightly or release workflow. That test should seed or emulate the registry at
10K devices and measure selector, label, pagination, and operation-resolution
behavior without launching 10K live edge runtimes. Real 10K runtime validation
should wait until server-side selector push-down exists, because the current
implementation still loads the full registry roster before matching.

## Release Notes

| Topic | Release Note |
|---|---|
| Event subscriptions | `subscribe("event(...)")` is live by event name and includes late-joining devices that emit those event names. `subscribe("device(...).event(...)")` snapshots the resolved device set at subscription time. |
| Predicate support | `where` predicates require predicate support at the dispatcher and participating edges. Missing edge support logs a startup warning and fails closed for `where` broadcasts. Predicate evaluation is bounded and fail-closed on timeout. |
| Discovery scale | The current implementation is validated with 200 real simulated devices in PR/CI. Server-side selector push-down remains future work for larger production-scale fleets. |
| CLI migration | Local mDNS scanning is `devctl mdns-scan` or `devctl scan`. Selector-based fleet discovery uses `devctl discover`. |
| Extension metadata | Protocol/vendor-specific schema extensions are treated as opaque by Device Connect and should be tested by the owning protocol integration. |
| ESTOP | The `function(estop)` selector pattern is covered for API discovery and fan-out. This is not a safety certification of physical devices, policy enforcement, or fail-safe behavior. |
