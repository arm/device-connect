# device-connect integration tests

Cross-package integration tests for the Device Connect monorepo (`packages/device-connect-sdk`, `packages/device-connect-server`, `packages/device-connect-agent-tools`).

## Prerequisites

Docker and Docker Compose for infrastructure (NATS, etcd, device-registry).

```bash
pip install -e ../packages/device-connect-sdk -e "../packages/device-connect-server[all]" -e "../packages/device-connect-agent-tools[strands]" -r requirements.txt
```

## Running tests

Start infrastructure first:

```bash
docker compose -f docker-compose-itest.yml up -d
```

### Unit tests (per-package, no Docker needed)

```bash
# device-connect-sdk
cd ../packages/device-connect-sdk && pytest tests/ -v

# device-connect-agent-tools
cd ../packages/device-connect-agent-tools && pytest tests/test_connection_unit.py tests/test_tools_unit.py -v

# device-connect-server
cd ../packages/device-connect-server && pytest tests/ -v
```

### Integration tests (Tier 1 — no LLM, fast)

```bash
pytest tests/ -v -m "not llm" --timeout=60
```

### Messaging conformance tests

```bash
pytest tests/test_messaging_conformance.py -v -m conformance
```

### LLM integration tests (Tier 2 — requires API key)

```bash
OPENAI_API_KEY="sk-..." pytest tests/ -v -m llm --timeout=120
```

### Stop infrastructure

```bash
docker compose -f docker-compose-itest.yml down -v
```

Set `ITEST_KEEP_INFRA=1` to keep containers running between test runs.

## Test markers

| Marker | Description |
|---|---|
| `integration` | Requires Docker infrastructure (auto-applied) |
| `llm` | Requires `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` |
| `slow` | Takes > 30 seconds |
| `conformance` | Messaging backend conformance (parameterized over NATS/MQTT/Zenoh) |

## Structure

- `drivers/` — Simulated device drivers (camera, robot, sensor) using `device_connect_sdk`
- `fixtures/` — Test infrastructure, device factory, event capture/inject, orchestrators
- `tests/` — Test files organized by tier and communication pattern (D2O, D2D, tools, LLM)

## Adding a new messaging backend (e.g. Zenoh)

1. Implement `MessagingClient` in `device-connect-sdk`
2. Add the backend's Docker service to `docker-compose-itest.yml`
3. Add `"zenoh"` to the `@pytest.fixture(params=[...])` in `test_messaging_conformance.py`
4. Run: `pytest tests/test_messaging_conformance.py -v -k zenoh`
