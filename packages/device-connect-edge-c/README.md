<!-- SPDX-License-Identifier: Apache-2.0 -->
# device-connect-edge-c

The **Device Connect edge SDK in C** — the device side. A C analogue of
`device-connect-edge`: write a driver (RPC functions + events + identity), run
it under the runtime, and it connects to NATS, registers with the portal,
serves commands, and keeps its lease alive with a heartbeat.

This is the C counterpart of the Python edge SDK, built in the same spirit as
the MHP C SDK's wire node (it reuses that node's proven JSON / JSON-RPC /
NATS-transport / security modules).

## What it does

- **Driver** (`dc/driver.h`): register `@rpc`-style functions by name, advertise
  events, set identity/status. DC RPC semantics: a command on
  `device-connect.{tenant}.{id}.cmd` is JSON-RPC where `method` *is* the
  function name and `params` are the arguments; the reply carries the raw
  return value as `result`.
- **Runtime** (`dc/runtime.h`): connect (NATS, via cnats), serve the cmd
  subject, `registerDevice` to `device-connect.{tenant}.registry`, publish a
  heartbeat every `device_ttl/3` to keep the lease (so the portal shows the
  device **online**), answer `requestRegistration` pulls, re-register on
  reconnect, plus `invoke_remote` (D2D) and `emit` (events).
- **Credentials**: consumes the portal's native `*.creds.json` directly
  (auto-converts to an nsc-chained creds for cnats) and auto-detects
  `device_id` / `tenant` from it. An nsc `*.creds` is also accepted.

## Build

Requires the NATS C client (cnats).

```
make NATS_CFLAGS="-I/opt/homebrew/include" NATS_LIBS="-L/opt/homebrew/lib -lnats"
make test
```

Produces `libdc_edge.a` + the `temp_sensor` example.

## Run

```
NATS_CREDENTIALS_FILE=./alpha-temp-001.creds.json \
  ./temp_sensor --server nats://portal.deviceconnect.dev:4222 --device-ttl 30
```

Verified end-to-end against a live DC portal (tenant `alpha`): 3 C devices
provisioned, registered, **online (sustained by heartbeat)**, discovered and
invoked via the C agent-tools (`get_reading`/`set_target`), with `-32601`
/`-32602` error semantics propagating.

## Scope / not yet

- Transport: NATS (cnats). MQTT/Zenoh slot into the `dc/transport.h` vtable.
- D2D presence collector, `@periodic`, and `@on` subscriptions are not yet
  wired (the runtime serves portal-mode register+heartbeat+cmd+invoke_remote).
- Security: TLS + JWT/NKey via cnats; mandate verification is out of scope.
