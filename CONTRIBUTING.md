# Contributing to Device Connect

Thank you for your interest in contributing to Device Connect! This guide will help you get started.

## Prerequisites

- Python 3.10+
- Docker & Docker Compose v2
- Git

## Development Setup

```bash
git clone https://github.com/arm/device-connect.git
cd device-connect

python3 -m venv .venv
source .venv/bin/activate

# Install all packages in editable mode
pip install -e packages/device-connect-edge
pip install -e "packages/device-connect-server[all]"
pip install -e "packages/device-connect-agent-tools[strands]"
```

## Monorepo Structure

| Package | Path | Description |
|---------|------|-------------|
| `device-connect-edge` | `packages/device-connect-edge/` | Edge SDK for building devices |
| `device-connect-server` | `packages/device-connect-server/` | Server runtime, registry, CLIs |
| `device-connect-agent-tools` | `packages/device-connect-agent-tools/` | AI agent integration (Strands, LangChain, MCP) |
| Integration tests | `tests/` | Cross-package integration tests |

See the [README](README.md) for the full architecture overview.

## Development Workflow

### 1. Create a branch

```bash
git checkout main && git pull origin main
git checkout -b feature/your-feature-name
# or: git checkout -b fix/issue-number-description
```

### 2. Make changes

- Follow existing patterns (`DeviceDriver`, `@rpc`/`@emit` decorators, `DeviceRuntime`)
- Add tests for new functionality
- Update documentation if needed

### 3. Commit

```bash
git add <specific-files>
git commit -m "feat: add device discovery API

- Implement mDNS-based device discovery
- Add discovery CLI command

Closes #123"
```

Commit message prefixes: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`

### 4. Open a pull request

```bash
git push origin feature/your-feature-name
```

Open a PR on GitHub targeting `main`. Include a description of what changed, why, and how to test it.

## Running Tests

### Unit tests (no Docker needed)

```bash
# SDK
cd packages/device-connect-edge && python3 -m pytest tests/ -v

# Server
cd packages/device-connect-server && python3 -m pytest tests/ -v

# Agent tools
cd packages/device-connect-agent-tools && python3 -m pytest tests/test_connection_unit.py tests/test_tools_unit.py -v
```

### Integration tests (require Docker)

```bash
cd tests
docker compose -f docker-compose-itest.yml up -d
DEVICE_CONNECT_ALLOW_INSECURE=true python3 -m pytest tests/ -v -m "not llm"
docker compose -f docker-compose-itest.yml down -v
```

See [tests/README.md](tests/README.md) for the full test matrix.

## Coding Standards

- **Style**: PEP 8, enforced with `ruff`
- **Line length**: 120 characters
- **Type hints**: Use throughout public APIs
- **Naming**: `snake_case` for functions/variables, `PascalCase` for classes, `UPPER_CASE` for constants, `camelCase` for event names (e.g., `taskComplete`, `plateGrasped`)
- **Docstrings**: Google-style
- **Tests**: pytest-asyncio with `asyncio_mode = auto`, mock `@emit` methods with `AsyncMock`

## Pull Request Checklist

- [ ] All unit tests pass
- [ ] Integration tests pass (if applicable)
- [ ] New code has tests
- [ ] No credentials or secrets in code
- [ ] Commit messages follow conventional format
- [ ] No merge conflicts with `main`

## Security

- Never commit secrets (API keys, passwords, `.env` files, private keys)
- Use environment variables for sensitive configuration
- Report security vulnerabilities privately — see [SECURITY.md](SECURITY.md)

## License

By contributing, you agree that your contributions will be licensed under the [Apache License 2.0](LICENSE).
