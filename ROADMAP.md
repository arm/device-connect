# Roadmap Highlights

## P0

- **RPC middleware chain** — composable interceptor pipeline for the dispatch path, enabling pluggable auth, validation, rate limiting, and custom cross-cutting concerns.
- **Default-deny ACL and parameter validation** — wire ACL enforcement into the RPC dispatch path, default to deny-all, and validate inbound parameters against `@rpc` schemas before handlers execute.
- **AI agent safety layer** — rate limiting, operation classification (read-only vs. actuating vs. destructive), function allow/deny lists, and `require_approval` enforcement with a pluggable human-approval backend.
- **TLS-by-default** — implement TLS for commissioning and D2D mode, replace the global `DEVICE_CONNECT_ALLOW_INSECURE` bypass with per-transport opt-out flags, and add application-layer auth for Zenoh.

## P1

- **Decision-time provenance** — model identity, nonces, replay protection, and operator metadata on agent-initiated RPCs; verifiable audit trail linking perception, decision, consent, and execution.
- **Extensibility** — expose internal lifecycle callbacks (reconnect, error, state-change) to driver authors; entry-points plugin registry for agent-tool adapters.
- **Full cryptographic provenance (CDCP)** — signed content manifests on sensor data, cryptographic decision authentication, consent binding, and tamper-evident audit receipts for regulated IoT, medical, and industrial deployments.
