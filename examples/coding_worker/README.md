# Coding Worker — remote coding-agent dispatch over Device Connect

A generic worker that exposes a local coding CLI (codex, claude, aider, …)
as a Device Connect device. A dispatcher agent running elsewhere can
hand off a prompt, let the worker run the CLI, push a feature branch,
and be notified when the work is done.

No custom driver code. Two commands on the dispatcher, two on the worker.

## Runtime flow

```
dispatcher agent (codex / claude-code on server)
      │
      │  MCP tools: list_devices, invoke_device, subscribe_events
      ▼
device-connect-agent-tools MCP bridge
      │
      │  JSON-RPC over Zenoh / NATS / MQTT
      ▼
coding-worker process (this package, running on the Pi)
      │
      │  subprocess: {--exec-cmd}  (codex / claude / aider / …)
      ▼
 git commit + push feature/<task-id>   →   shared Git remote
      │
      │  @emit work_done {task_id, branch, sha, summary}
      ▼
dispatcher agent receives the event, fetches the branch
```

## What the user sees

```
user@server:~/shared-app $ codex
codex › MCP servers loaded: device-connect (4 tools)
❯ There's a flaky test in tests/auth/test_login.py — hand it off
  to the worker on my desk while I work on the dashboard branch.

codex › [tool] describe_fleet()  → 1 coding-worker: pi-desk
codex › [tool] invoke_device(pi-desk, dispatch, {prompt: "...", base_ref: "main"})
codex → accepted as T-42. I'll watch for work_done.
❯ meanwhile let me refactor the navbar…

   [event] pi-desk::work_done {
     task_id: "T-42", branch: "feature/T-42",
     sha: "a3f91c2",
     summary: "Fixed race in session teardown…"
   }

codex → pi-desk finished T-42. Want me to fetch feature/T-42?
❯ yes
```

## One-time setup

### Dispatcher side (server)

```bash
pipx install 'device-connect-agent-tools[mcp]'
```

Point codex (or any MCP-speaking agent) at the bridge. Example
`~/.codex/config.toml`:

```toml
[mcp_servers.device-connect]
command = "python"
args = ["-m", "device_connect_agent_tools.mcp"]
env = { TENANT = "alice", DEVICE_CONNECT_ALLOW_INSECURE = "true" }
```

### Worker side (Pi / workstation)

Three install options, pick one:

**a) pip / pipx (simplest if the Pi has Python + network).**

```bash
pipx install ./examples/coding_worker
# or from a git checkout:
pipx install 'git+https://github.com/arm/device-connect#subdirectory=examples/coding_worker'
```

**b) Single-file zipapp (provision from the dispatcher host, no pip on the Pi).**

Build once on your dev machine, scp to the Pi. The Pi only needs Python 3.9+.

```bash
pip install shiv
shiv -c coding-worker -o coding-worker.pyz \
  ./examples/coding_worker device-connect-edge

scp coding-worker.pyz pi-desk:~/
ssh pi-desk 'python3 ~/coding-worker.pyz --help'
```

**c) PyInstaller single binary (no Python on the Pi at all).** Build on a
host with the Pi's architecture (or cross-build):

```bash
pip install pyinstaller
pyinstaller --onefile --name coding-worker coding_worker.py
scp dist/coding-worker pi-desk:~/.local/bin/
```

## Running the worker

### Single repo (simplest)

```bash
DEVICE_CONNECT_ALLOW_INSECURE=true \
TENANT=alice \
coding-worker \
  --device-id pi-desk \
  --exec-cmd 'codex exec --full-auto {prompt}' \
  --repo-path ~/repos/shared-app
```

### Multiple repos (allowlist)

Pass `--repo NAME=PATH` once per repo the worker is allowed to operate
on. The caller picks one by name via the `repo` param on `dispatch()`.
Paths are resolved at startup; callers can't escape the list.

```bash
coding-worker \
  --device-id pi-desk \
  --exec-cmd 'codex exec --full-auto {prompt}' \
  --repo shared-app=~/repos/shared-app \
  --repo infra=~/repos/infra \
  --repo docs=~/repos/team-docs \
  --default-repo shared-app
```

### Substitute any coding CLI

```bash
# claude-code
--exec-cmd 'claude -p {prompt}'

# aider
--exec-cmd 'aider --message {prompt} --yes'
```

The worker shell-quotes the incoming `{prompt}` before substitution — do
**not** wrap `{prompt}` in your own quotes in the template.

## Picking the repo from the dispatcher side

The worker exposes `list_repos()` as an RPC, so any agent can discover
which repos are available without a config file on the server side:

```
codex › [tool] invoke_device(pi-desk, list_repos)
  → {"repos": [
       {"name": "shared-app", "path": "/home/pi/repos/shared-app"},
       {"name": "infra",      "path": "/home/pi/repos/infra"},
       {"name": "docs",       "path": "/home/pi/repos/team-docs"}],
     "default": "shared-app"}

codex › [tool] invoke_device(pi-desk, dispatch,
                             {repo: "infra", prompt: "add Terraform module..."})
```

If the caller omits `repo`, the worker uses `--default-repo` (or the
first `--repo` entry). If the caller passes an unknown name, the
dispatch is rejected with a clear error — no accidental writes to
unexpected paths.

**Concurrency note.** The current worker runs each task directly in the
repo's working copy, so two concurrent tasks targeting the **same
repo** will step on each other's `git checkout`. One task per repo at
a time is safe; parallel tasks across different repos are fine. If you
need parallel tasks on the same repo, extend the driver to use
`git worktree add` per task — roughly a 10-line change to `_run`.

## Multi-tenant isolation on a shared network

All Device Connect subjects are prefixed with `TENANT`. Different values
→ different namespaces → no cross-discovery.

### Tier 1 — cooperating friends, same LAN

Each person picks a unique tenant and sets it on both sides.

```bash
# alice (both dispatcher and pi)
export TENANT=alice

# bob (both dispatcher and pi)
export TENANT=bob
```

Alice's `list_devices()` only sees devices that announced themselves
under `device-connect.alice.*.presence`. Same for Bob.

**Caveat:** this is a logical namespace, not a cryptographic boundary.
With `DEVICE_CONNECT_ALLOW_INSECURE=true` and shared multicast, a curious
peer who knows Alice's tenant name can still subscribe to her subjects
and even publish commands. Fine for cooperating users; not fine if
anyone on the network is untrusted.

### Tier 2 — untrusted peers on the same network

Turn off D2D multicast and run a broker with per-tenant credentials. On
NATS this means one account per friend, with subject ACL restricted to
`device-connect.{their-tenant}.>`. On MQTT/Zenoh the equivalent is
per-client ACL rules.

On each side drop the per-tenant credentials file into
`security_infra/credentials/orchestrator.creds.json` (dispatcher) and
use `--nats-credentials-file` or env (`NATS_CREDENTIALS_FILE`) on the
worker. See `packages/device-connect-server/security_infra/README.md`
for generation.

Unset `DEVICE_CONNECT_ALLOW_INSECURE` and point both sides at the broker:

```bash
export NATS_URL=tls://broker.example:4222
export NATS_CREDENTIALS_FILE=/path/to/alice.creds
export NATS_TLS_CA_FILE=/path/to/ca-cert.pem
export TENANT=alice
```

Now the broker enforces that Alice's dispatcher can only talk to
Alice's workers — the tenant prefix is no longer an honor-system check.

### Tier 3 — self-service onboarding

For more than a handful of people, run the `device-connect-server`
multi-tenant portal (see
`packages/device-connect-server/device_connect_server/portal/README.md`).
Each friend signs up, the portal provisions their credentials and ACLs.
No per-tenant config files to hand-edit.

## RPC surface

Exposed by this worker:

| Function / event | Kind | Payload |
| --- | --- | --- |
| `list_repos()` | `@rpc` | → `{repos: [{name, path}], default}` |
| `dispatch(prompt, base_ref, task_id, repo)` | `@rpc` | → `{accepted, task_id, repo}` |
| `status(task_id)` | `@rpc` | → `{state, repo?, branch?, sha?, summary?, error?, category?, step?}` |
| `cancel(task_id)` | `@rpc` | → `{cancelled, reason?}` |
| `get_logs(task_id, tail=200)` | `@rpc` | → `{lines, truncated, total}` |
| `progress` | `@emit` | `{task_id, step, detail}` |
| `work_done` | `@emit` | `{task_id, branch, sha, summary}` |
| `work_failed` | `@emit` | `{task_id, error, category, step, log_tail}` |

## Failure reporting

When a task fails, the worker emits `work_failed` with a structured
classification so the dispatcher agent can decide what to do without a
second round-trip:

- `category` — fixed vocabulary: `precondition`, `agent_error`,
  `no_changes`, `conflict`, `auth`, `rate_limit`, `cancelled`, `unknown`.
- `step` — where it died: `checkout`, `agent`, `commit`, `push`.
- `log_tail` — last 20 lines of that task's combined stdout/stderr.

Per-task logs are kept on disk (default `~/.local/state/coding-worker/logs/<task_id>.log`).
Use `get_logs(task_id, tail=N)` to pull more context on demand — either
to peek at a running task, or to investigate after `work_failed`.

Suggested dispatcher policies:

| Category | Typical action |
| --- | --- |
| `no_changes` | Re-dispatch with a sharper prompt, or mark task done-nothing |
| `rate_limit` | Retry after backoff on same worker |
| `conflict` | Rebase on server side, re-dispatch, or escalate |
| `auth` | Surface to user — the Pi needs credentials refreshed |
| `precondition` | Check worker config (repo path, base ref) |
| `agent_error` | Inspect `log_tail` / `get_logs`; surface to user |
| `cancelled` | No action |
| `unknown` | Surface to user with `get_logs` output |

## Security notes

- The `--exec-cmd` template is trusted: it is set by the Pi operator,
  not by the dispatcher. Don't accept it from an RPC.
- The caller's `prompt` is passed through `shlex.quote()` before
  substitution, so shell metacharacters in the prompt are inert.
- `DEVICE_CONNECT_ALLOW_INSECURE=true` skips TLS and is only suitable
  for a trusted LAN. For anything else use Tier 2 or Tier 3 above.
- The worker runs whatever the coding CLI decides to run. Sandbox the
  host (dedicated user, restricted git remote, read-only system dirs)
  if that matters to you.
