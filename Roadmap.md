# Roadmap

## P0

1. **Default-deny ACL** — switch the ACL framework from its current permissive defaults (`visible_to: ["*"]`) to deny-by-default, so every device and caller must be explicitly allowed. Enforce RPC parameter validation against type-hint schemas at the dispatch boundary before a handler ever executes.

2. **AI agent safety layer** — add rate limiting, operation classification (read-only vs. actuating), and function allow/deny lists for agent-to-device calls. Wire up the existing `require_approval` field in `FunctionACL` to an actual approval workflow so destructive operations require human confirmation.

3. **TLS-by-default** — make TLS the default for commissioning and D2D mode instead of requiring `DEVICE_CONNECT_ALLOW_INSECURE=true`. Auto-generate self-signed certificates for local development and provide an explicit opt-out flag for test environments.

4. **PyPI publishing** — add CI/CD steps to build and publish `device-connect-sdk`, `device-connect-server`, and `device-connect-agent-tools` to PyPI on tagged releases. Include dependency lock files for reproducible installs across all three packages.

## P1

5. **Kubernetes manifests** — provide production-ready Helm charts for the Zenoh router, etcd, and registry stack. Include horizontal scaling, readiness/liveness probes, and high-availability deployment documentation to move beyond the current Docker Compose setup.

6. **Structured logging** — replace bare `logging` calls with structured JSON output across all packages, aligned with the existing MongoDB audit logger format. Add `/health` and `/readiness` HTTP endpoints to the registry and device runtime for Kubernetes and load-balancer integration.

7. **Decision-time provenance (CDCP)** — attach content manifests, decision authentication, consent binding, and tamper-evident audit receipts to every agent-to-actuator flow. Enable downstream systems to verify who requested an action, which model produced the decision, and whether the operator consented — critical for regulated IoT, medical, and industrial deployments.
