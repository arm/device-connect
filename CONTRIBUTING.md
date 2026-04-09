# Contributing to Device Connect

Thank you for your interest in contributing to Device Connect! This guide will help you get started.

## Prerequisites

- Python 3.11+
- Docker & Docker Compose v2
- Git
- [GitHub CLI](https://cli.github.com/) (`gh`) — optional but recommended

## Development Setup

### 1. Fork & clone (first time only)

The easiest path uses the [GitHub CLI](https://cli.github.com/):

```bash
gh repo fork arm/device-connect --clone
cd device-connect
```

This forks the repo under your account and clones it with `origin` pointing to your fork and `upstream` pointing to `arm/device-connect`.

<details>
<summary>Manual setup (without <code>gh</code>)</summary>

1. Fork via the GitHub UI
2. Clone your fork and add the upstream remote:

```bash
git clone https://github.com/<you>/device-connect.git
cd device-connect
git remote add upstream https://github.com/arm/device-connect.git
```

</details>

### 2. Install dependencies

```bash
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
git fetch upstream
git checkout -b feature/your-feature-name upstream/main
# or: git checkout -b fix/issue-number-description upstream/main
```

### 2. Make changes

- Follow existing patterns (`DeviceDriver`, `@rpc`/`@emit` decorators, `DeviceRuntime`)
- Add tests for new functionality
- Update documentation if needed

### 3. Lint & test

```bash
# Lint (must pass before opening a PR)
ruff check packages/ tests/

# Unit tests — run the package(s) you changed
pytest packages/device-connect-edge/tests/ -v
pytest packages/device-connect-server/tests/ -v
pytest packages/device-connect-agent-tools/tests/test_connection_unit.py \
       packages/device-connect-agent-tools/tests/test_tools_unit.py \
       packages/device-connect-agent-tools/tests/test_langchain_adapter.py \
       packages/device-connect-agent-tools/tests/test_strands_adapter.py -v
```

See [Running Tests](#running-tests) below for integration tests.

### 4. Commit

```bash
git add <specific-files>
git commit -m "feat: add device discovery API

- Implement mDNS-based device discovery
- Add discovery CLI command

Closes #123"
```

Commit message prefixes: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`

### 5. Open a pull request

```bash
gh pr create --fill
```

This pushes your branch to your fork and opens a PR against `arm/device-connect:main` in one step.

<details>
<summary>Manual setup (without <code>gh</code>)</summary>

```bash
git push origin feature/your-feature-name
```

Then open a PR on GitHub from your fork targeting `arm/device-connect:main`.

</details>

## Running Tests

### Unit tests (no Docker needed)

Run from the repository root:

```bash
# Edge SDK
pytest packages/device-connect-edge/tests/ -v

# Server
pytest packages/device-connect-server/tests/ -v

# Agent tools
pytest packages/device-connect-agent-tools/tests/test_connection_unit.py \
       packages/device-connect-agent-tools/tests/test_tools_unit.py \
       packages/device-connect-agent-tools/tests/test_langchain_adapter.py \
       packages/device-connect-agent-tools/tests/test_strands_adapter.py -v
```

### Integration tests (require Docker)

```bash
cd tests
docker compose -f docker-compose-itest.yml up -d
DEVICE_CONNECT_ALLOW_INSECURE=true pytest -v -m "not llm"
docker compose -f docker-compose-itest.yml down -v
```

See [tests/README.md](tests/README.md) for the full test matrix.

## Coding Standards

- **Style**: PEP 8, enforced with [`ruff`](ruff.toml)
- **Line length**: 120 characters (configured in [`ruff.toml`](ruff.toml))
- **Type hints**: Use throughout public APIs
- **Naming**: `snake_case` for functions/variables, `PascalCase` for classes, `UPPER_CASE` for constants, `camelCase` for event names (e.g., `taskComplete`, `plateGrasped`)
- **Docstrings**: Google-style
- **Tests**: pytest with pytest-asyncio; `asyncio_mode` varies by package (`auto` in server and integration tests, `strict` in agent-tools). Mock `@emit` methods with `AsyncMock`.

## Pull Request Checklist

- [ ] `ruff check` passes with no errors
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
