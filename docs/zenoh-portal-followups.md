# Zenoh DC-Portal enablement — follow-on work

Non-blocking items deferred out of the `harden/zenoh-portal-security` branch.
The enablement itself is complete and verified end-to-end; these are quality /
consistency improvements to pick up in a later PR.

## 1. Normalize `invoke_remote` / `request()` failure exceptions across backends

**Problem.** The same logical failure raises different exception *types*
depending on the messaging backend, so portable driver code cannot reliably
branch on the cause of an `invoke_remote()` failure.

Specifically, "target is not present / nobody is subscribed":

- **NATS** (`messaging/nats_adapter.py:425-434`): the NATS "no responders"
  reply is caught by the generic handler and raised as **`PublishError`**
  (`"Request failed: ... no responders"`); a real timeout
  (`asyncio.TimeoutError`) raises **`RequestTimeoutError`**.
- **Zenoh** (`messaging/zenoh_adapter.py:829-849`): exhausting retries with no
  reply raises **`RequestTimeoutError`** (message ends `"(no responders)"`);
  a query-error reply raises **`PublishError`**.

So a device that is simply absent yields `PublishError` on NATS but
`RequestTimeoutError` on Zenoh. A driver author following the playbook (which
documents both shapes) must catch backend-specific types or fall back to broad
`except Exception` + string matching — which is exactly what the demo drivers do
today.

**Proposed fix.** Introduce a dedicated, backend-neutral
`NoRespondersError` (subclass of `MessagingError`, sibling of
`RequestTimeoutError`) in `device_connect_edge/messaging/exceptions.py`, and map
to it consistently in every adapter's `request()`:

- no available responder / no queryable / NATS "no responders"  -> `NoRespondersError`
- deadline exceeded with a responder present                    -> `RequestTimeoutError`
- transport/publish/query-reply error                           -> `PublishError`

Then driver code (and `DeviceDriver.invoke_remote`) can branch on a single,
portable taxonomy regardless of backend. Keep the current types as base classes
so existing `except PublishError/RequestTimeoutError` callers don't break.

**Scope.** `messaging/exceptions.py` (new type) + `nats_adapter.py`,
`zenoh_adapter.py`, and `mqtt_adapter.py` `request()` paths; a cross-backend
unit test asserting the same failure raises the same type; and a one-line
update to AGENTS §4's `invoke_remote` note (it currently lists the per-backend
shapes).
