# Coding Worker — remote coding-agent dispatch over Device Connect

A generic worker that exposes a local coding CLI (codex, claude, aider, …)
as a Device Connect device. A dispatcher agent running elsewhere can hand
off a prompt, let the worker run the CLI, push a feature branch, and be
notified when the work is done.

No custom driver code on either end. One command to bring up a remote
worker from your laptop.

## Quick start

From a fresh clone on your dispatcher machine:

```bash
./examples/coding_worker/provision.sh user@host
```

This bootstraps the remote host over SSH: installs Python 3.11 via `uv`,
installs `device-connect-edge` into a dedicated venv, uploads the worker
script, installs a systemd user service so the worker auto-restarts and
survives reboots, and — if you didn't give it a real coding CLI yet —
wires up a shell stub + throwaway test repo so you can exercise the
whole round-trip immediately.

During provisioning, the script also snapshots common Codex/OpenAI proxy
and CA environment variables from the remote user's login shell into
`~/.coding-worker/agent-env.sh`, and the service sources that file on
startup. If you change those env vars later, rerun `provision.sh` or edit
`~/.coding-worker/agent-env.sh` on the remote.

At the end it prints the exact codex MCP config block to paste into
`~/.codex/config.toml`. After that you can dispatch tasks from codex.

### Flags you'll likely want

```bash
./examples/coding_worker/provision.sh user@host \
  --device-id jetson-01 \
  --tenant alice \
  --repo ~/code/my-project \
  --exec-cmd 'codex exec --full-auto {prompt}'
```

| Flag | Purpose | Default |
| --- | --- | --- |
| `--device-id` | How the worker registers on the mesh | `<hostname>-worker` |
| `--tenant` | Namespace — different tenants don't see each other | `default` |
| `--port` | TCP port for Zenoh listener | `7447` |
| `--exec-cmd` | Coding-agent template; `{prompt}` substituted in | shell stub for testing |
| `--repo PATH` | Existing working copy already on the remote | — |
| `--seed-from PATH` | Local repo on dispatcher (under `$HOME`); mirrors path on remote, pushes all branches, auto-adds a `jetson` git remote on your local repo. Worker commits locally; you `git fetch jetson`. | — |
| `--seed-from-url URL` | Git URL `git clone`d on the remote. Worker pushes feature branches back to URL. | — |
| `--at PATH` | Override remote destination for `--seed-from` / `--seed-from-url` (rel to remote `$HOME`, or absolute). | — |
| `--uninstall` | Stop the service and remove the install | — |

`--repo`, `--seed-from`, and `--seed-from-url` are mutually exclusive. If
none of them is given, the script creates a throwaway test repo at
`~/work/test-repo` so the round trip works immediately.

## Run an end-to-end example

The walkthrough below dispatches a task from your laptop to a remote worker
running real `codex`, then fetches the result. Replace `user@host` with
your own (e.g. `sourav@10.104.39.11`).

### 1. Provision the worker

From a checkout of this repo on your laptop:

```bash
./examples/coding_worker/provision.sh user@host \
  --device-id jetson-01 \
  --tenant test \
  --seed-from ~/workplace/my-project \
  --exec-cmd '/home/user/.local/bin/codex exec --full-auto {prompt}'
```

What this does:

- Installs Python 3.11 + `device-connect-edge` on the remote in
  `~/.coding-worker/venv/`.
- Mirrors `~/workplace/my-project` to `~/workplace/my-project` on the
  remote (same path under `$HOME`), pushing every local branch.
- Adds a git remote called `jetson` on your local repo pointing at the
  Jetson's working copy, so you can `git fetch jetson` later.
- Snapshots your remote login shell's proxy/CA env vars (e.g.
  `OPENAI_BASE_URL`, `HTTPS_PROXY`, `OPENAI_CA_CERT_PATH`,
  `REQUESTS_CA_BUNDLE`) into `~/.coding-worker/agent-env.sh` so the
  systemd-run worker can reach the same upstreams as your interactive
  shell. Re-run `provision.sh` if those values change.
- Installs and starts a systemd user service called `coding-worker`.
- Prints the exact MCP config block to paste into your dispatcher's
  codex config.

The absolute path on `--exec-cmd` matters: systemd-user services don't
inherit your interactive `PATH`. Find it with `ssh user@host 'which codex'`.

### 2. Add the MCP config to your dispatcher

Paste the block printed at the end of step 1 into `~/.codex/config.toml`
(or your client's equivalent), then restart the client. It looks like:

```toml
[mcp_servers.device-connect]
command = "/path/to/device-connect/.venv/bin/python"
args = ["-m", "device_connect_agent_tools.mcp"]

[mcp_servers.device-connect.env]
DEVICE_CONNECT_ALLOW_INSECURE = "true"
MESSAGING_BACKEND = "zenoh"
ZENOH_CONNECT = "tcp/<jetson-ip>:7447"
DEVICE_CONNECT_DISCOVERY_MODE = "d2d"
TENANT = "test"
```

### 3. Dispatch from your dispatcher (codex / claude-code / …)

Run the dispatcher CLI **from the local repo that the worker is
mirroring** so its branch context matches the worker's:

```bash
cd ~/workplace/my-project
codex
```

Then a natural prompt:

```
Use device-connect to run the unit tests on my jetson worker.
Write a TEST-REPORT.md with the command, pass/fail counts, and any
failing test excerpts. Commit it. Tell me when it's done, or what
went wrong if it fails.
```

The dispatcher should choose the right MCP tools itself:
`describe_fleet` to find the worker, `list_repos` to see which repo is
exposed, `dispatch` with a sensible `base_ref` (typically your current
branch since `--seed-from` mirrored it), then `wait_for_event` to block
on completion (race-safe; one tool call regardless of task duration).
On `work_failed`, the dispatcher pulls more context via
`get_logs(task_id)`. See [WALKTHROUGH.md](WALKTHROUGH.md) for sequence
diagrams of how these fit together.

### 4. Fetch the worker's result

When the dispatcher reports `work_done`:

```bash
cd ~/workplace/my-project
git fetch jetson
git log --oneline jetson/feature/<task-id> -3
git show jetson/feature/<task-id>:TEST-REPORT.md
```

`feature/<task-id>` is the branch name reported in the `work_done`
event payload. With `--seed-from`, the worker commits that branch
locally on the remote; `git fetch jetson` pulls it down to your
laptop without ever round-tripping through GitHub.

### 5. Tear down

```bash
./examples/coding_worker/provision.sh user@host --uninstall
```

This stops the service and removes `~/.coding-worker/`. The mirrored
working copy at `~/workplace/my-project` and the local `jetson` git
remote are intentionally left in place so you can still inspect
history; remove them by hand if you want a true clean slate.

## Runtime flow

```
dispatcher agent (codex / claude-code on your laptop)
      │
      │  MCP tools: describe_fleet, list_devices, invoke_device,
      │             get_device_functions  +  events resource
      ▼
device-connect-agent-tools MCP bridge
      │
      │  JSON-RPC over Zenoh / NATS / MQTT
      ▼
coding-worker process (this package, running on the remote)
      │
      │  subprocess: {--exec-cmd}  (codex / claude / aider / …)
      ▼
 git commit feature/<task-id>
      │
      │  Push behavior depends on how the worker was provisioned:
      │    • --seed-from        → no push; dispatcher does `git fetch jetson`
      │    • --seed-from-url    → push to URL (e.g. GitHub) as origin
      │    • --repo PATH        → push to whatever origin is configured
      ▼
 @emit work_done {task_id, branch, sha, summary}
      │
      ▼
dispatcher agent learns about completion. The bridge exposes a
`wait_for_event` tool that blocks for the matching event with a single
tool call (race-safe via an in-memory ring buffer). On failure the
agent pulls more context with `get_logs`. The bridge also publishes
`notifications/resources/updated` on `events://devices/<id>/latest`
for MCP clients that subscribe to resources natively.
```

See [WALKTHROUGH.md](WALKTHROUGH.md) for what a single dispatch looks
like end to end, including how the dispatcher learns about failures.

## Provisioning multiple devices

Run the provision script once per device. Each invocation is independent
and idempotent.

```bash
./provision.sh user@jetson-1 --device-id jetson-01 --tenant alice
./provision.sh user@jetson-2 --device-id jetson-02 --tenant alice
./provision.sh user@mac-mini --device-id mac-mini-01 --tenant alice
```

The devices all share a tenant (`alice`) so the dispatcher sees them as
one fleet. Device ids must be unique within a tenant; everything else can
repeat.

### How the dispatcher reaches more than one device

There are two deployment shapes. Pick based on fleet size.

#### Shape A — a handful of devices, peer-to-peer

Each worker listens on TCP 7447. The dispatcher's MCP config lists
every worker as a Zenoh connect endpoint:

```toml
[mcp_servers.device-connect.env]
ZENOH_CONNECT = "tcp/10.0.0.11:7447|tcp/10.0.0.12:7447|tcp/10.0.0.13:7447"
DEVICE_CONNECT_DISCOVERY_MODE = "d2d"
TENANT = "alice"
```

Works up to a handful of devices on a flat network. Every worker is a
direct peer of every dispatcher. No central service, no reboot fragility.

#### Shape B — fleet of devices, central router

Run one `zenohd` (or any Zenoh router) somewhere always-on. Every worker
connects to it instead of listening; every dispatcher connects to the
same address. Adding or removing a device doesn't touch the dispatcher's
config.

Worker side — pass `--connect` in the future (or set `ZENOH_CONNECT` in
the systemd unit) to point at the router instead of listening. Example
ops pattern:

```
                         ┌─────────────┐
                         │  zenohd     │  router
                         │  :7447      │
                         └──────┬──────┘
                    ┌───────────┼───────────┐
                    │           │           │
             ┌──────┴───┐ ┌─────┴────┐ ┌────┴─────┐
             │ worker A │ │ worker B │ │ worker C │
             └──────────┘ └──────────┘ └──────────┘
                    ▲           ▲           ▲
                    └───────────┼───────────┘
                                │
                         ┌──────┴──────┐
                         │ dispatcher  │  codex CLI + MCP bridge
                         │  (laptop)   │
                         └─────────────┘
```

Multi-dispatcher also becomes natural: several laptops all connect to
the same router and see the same fleet (optionally filtered by tenant).

### Keeping tenants straight when you have collaborators

If several people share a physical network, put each person in their
own tenant:

```bash
# alice's jetson
./provision.sh user@jetson-a --tenant alice --device-id jetson-a

# bob's jetson
./provision.sh user@jetson-b --tenant bob --device-id jetson-b
```

Alice's dispatcher only sees alice's devices; same for bob. This is a
logical namespace — see "Multi-tenant isolation" below for how to make
it a hard boundary with credentials.

## Troubleshooting

- **Service logs on the remote**
  ```bash
  ssh user@host 'tail -f ~/.coding-worker/worker.log'
  ```
- **Service state**
  ```bash
  ssh user@host 'systemctl --user status coding-worker'
  ```
- **Worker listening?**
  ```bash
  ssh user@host "ss -tln | grep ':7447 '"
  ```
- **Remove everything the provision script installed**
  ```bash
  ./provision.sh user@host --uninstall
  ```

## Picking the repo from the dispatcher side

The worker exposes `list_repos()` so any agent can discover which repos
are available without config on the dispatcher:

```
codex › [tool] invoke_device(jetson-01, list_repos)
  → {"repos": [
       {"name": "shared-app", "path": "/home/user/repos/shared-app"},
       {"name": "infra",      "path": "/home/user/repos/infra"},
       {"name": "docs",       "path": "/home/user/repos/team-docs"}],
     "default": "shared-app"}

codex › [tool] invoke_device(jetson-01, dispatch,
                             {repo: "infra", prompt: "add Terraform module..."})
```

If the caller omits `repo`, the worker uses `--default-repo` (or the
first `--repo` entry). Unknown names are rejected with a clear error —
no accidental writes to unexpected paths.

To allow multiple repos on one worker, the worker CLI accepts
`--repo NAME=PATH` repeatedly; the provision script doesn't surface
that yet, so use the manual systemd unit (see below), or edit
`~/.coding-worker/run.sh` on the remote after provisioning.

**Concurrency.** The worker runs each task directly in the repo's
working copy, so two concurrent tasks targeting the **same repo** will
collide on `git checkout`. One task per repo at a time is safe; parallel
tasks across different repos are fine. If you need parallel tasks on the
same repo, extend `_run()` to use `git worktree add` per task (~10 lines).

## Multi-tenant isolation on a shared network

All Device Connect subjects are prefixed with `TENANT`. Different values
→ different namespaces → no cross-discovery.

### Tier 1 — cooperating users, same LAN

Each person picks a unique tenant and sets it on both sides. The
`--tenant` flag on `provision.sh` handles the worker side; `TENANT=<name>`
in your MCP config handles the dispatcher side.

Alice's `list_devices()` only sees devices that announced themselves
under `device-connect.alice.*.presence`. Same for Bob.

**Caveat:** this is a logical namespace, not a cryptographic boundary.
With `DEVICE_CONNECT_ALLOW_INSECURE=true`, a curious peer who knows the
tenant name can still subscribe to those subjects and even publish
commands. Fine for cooperating users on a trusted network; not fine if
anyone on the network is untrusted.

### Tier 2 — untrusted peers on the same network

Turn off insecure mode and run a broker with per-tenant credentials. On
NATS this means one account per user, with subject ACL restricted to
`device-connect.{their-tenant}.>`. On MQTT/Zenoh the equivalent is
per-client ACL rules.

On each side drop the per-tenant credentials file into
`security_infra/credentials/orchestrator.creds.json` (dispatcher) and
use `NATS_CREDENTIALS_FILE` env on the worker. See
`packages/device-connect-server/security_infra/README.md` for generation.

```bash
export NATS_URL=tls://broker.example:4222
export NATS_CREDENTIALS_FILE=/path/to/alice.creds
export NATS_TLS_CA_FILE=/path/to/ca-cert.pem
export TENANT=alice
```

The broker now enforces that alice's dispatcher can only talk to alice's
workers — the tenant prefix is no longer honor-system.

### Tier 3 — self-service onboarding

For more than a handful of people, run the `device-connect-server`
multi-tenant portal (see
`packages/device-connect-server/device_connect_server/portal/README.md`).
Each user signs up; the portal provisions their credentials and ACLs.

## RPC surface

Exposed by this worker:

| Function / event | Kind | Payload | Used for |
| --- | --- | --- | --- |
| `list_repos()` | `@rpc` | → `{repos: [{name, path}], default}` | Discover what the worker can edit before dispatching |
| `dispatch(prompt, base_ref, task_id, repo)` | `@rpc` | → `{accepted, task_id, repo}` | Hand a coding task off to the worker; returns immediately |
| `task_status(task_id)` | `@rpc` | → `{state, repo?, branch?, sha?, summary?, error?, category?, step?}` | Fallback / explicit poll: "is task X still running, and what's its final state?" |
| `cancel(task_id)` | `@rpc` | → `{cancelled, reason?}` | Stop a running task cleanly |
| `get_logs(task_id, tail=200)` | `@rpc` | → `{lines, truncated, total}` | Peek at a running task or pull diagnostic context after `work_failed` |
| `progress` | `@emit` | `{task_id, step, detail}` | Streaming step markers (`checkout` / `agent` / `commit` / `push`) |
| `work_done` | `@emit` | `{task_id, branch, sha, summary}` | Terminal: task succeeded; feature branch is committed |
| `work_failed` | `@emit` | `{task_id, error, category, step, log_tail}` | Terminal: task failed; payload is structured for routing |

Named `task_status` (not `status`) because `DeviceDriver` reserves the
`status` attribute for the device-level health property.

### How the dispatcher uses each piece

For a typical "dispatch task → wait for result → report back" flow,
the dispatcher mostly uses three things:

1. **`invoke_device(jetson-01, dispatch, ...)`** — start the task. Returns
   in milliseconds with `{accepted: true, task_id: ...}`.
2. **`wait_for_event(jetson-01, event_name="work_done", match_params={task_id})`**
   on the MCP bridge — block until the task emits a terminal event. One
   tool call. Race-safe: returns immediately if the worker fired the
   event before the wait started (common for fast tasks). Run a parallel
   call for `event_name="work_failed"` to catch either outcome.
3. **`invoke_device(jetson-01, get_logs, {task_id, tail: N})`** — only when
   you need more context than `work_failed.log_tail` provides (which is
   capped at 20 lines).

`task_status` and `cancel` exist for the off-cases:

- **`task_status`** is the explicit fallback when `wait_for_event` times
  out without a match: "did the task actually finish, or is it stuck?"
  Also useful if the dispatcher restarts and forgets which tasks were in
  flight — you can re-query by `task_id`.
- **`cancel`** is for the user-says-stop case. Worker raises
  `asyncio.CancelledError`, emits `work_failed` with `category=cancelled`.

`progress` events arrive between dispatch and the terminal event. The
dispatcher rarely needs to react to them, but they're how you'd build a
live progress UI if you wanted one — every event lands in the bridge's
ring buffer and is queryable via `wait_for_event` (with no
`event_name` filter) or via the events resource.

## Failure reporting

When a task fails, the worker emits `work_failed` with a structured
classification so the dispatcher can decide what to do without a second
round-trip:

- `category` — fixed vocabulary: `precondition`, `agent_error`,
  `no_changes`, `conflict`, `auth`, `rate_limit`, `cancelled`, `unknown`.
- `step` — where it died: `checkout`, `agent`, `commit`, `push`.
- `log_tail` — last 20 lines of that task's combined stdout/stderr.

Per-task logs are on disk at `~/.local/state/coding-worker/logs/<task_id>.log`.
Use `get_logs(task_id, tail=N)` to pull more context on demand — either
to peek at a running task, or to investigate after `work_failed`.

Suggested dispatcher policies:

| Category | Typical action |
| --- | --- |
| `no_changes` | Re-dispatch with a sharper prompt, or mark task done-nothing |
| `rate_limit` | Retry after backoff on same worker |
| `conflict` | Rebase on server side, re-dispatch, or escalate |
| `auth` | Surface to user — remote needs credentials refreshed |
| `precondition` | Check worker config (repo path, base ref) |
| `agent_error` | Inspect `log_tail` / `get_logs`; surface to user |
| `cancelled` | No action |
| `unknown` | Pull `get_logs`, surface to user |

## Security notes

- The `--exec-cmd` template is trusted: it's set by the worker operator,
  not by the dispatcher. Don't accept it from an RPC.
- The caller's `prompt` is passed through `shlex.quote()` before
  substitution, so shell metacharacters in the prompt are inert.
- `DEVICE_CONNECT_ALLOW_INSECURE=true` skips TLS and is only suitable
  for a trusted LAN. For anything else use Tier 2 or Tier 3 above.
- The worker runs whatever the coding CLI decides to run. Sandbox the
  host (dedicated user, restricted git remote, read-only system dirs)
  if that matters to you.

## Manual setup (without provision.sh)

The script is the recommended path. If you want to do it yourself:

- **pipx** (simplest if the remote has Python 3.11 + network):
  ```bash
  pipx install ./examples/coding_worker
  ```
- **shiv zipapp** (single-file, no pip on the remote):
  ```bash
  pip install shiv
  shiv -c coding-worker -o coding-worker.pyz \
    ./examples/coding_worker device-connect-edge
  scp coding-worker.pyz user@host:~/
  ssh user@host 'python3 ~/coding-worker.pyz --help'
  ```
- **PyInstaller single binary** (no Python at all on the remote): build on
  the target architecture, scp the binary.

Then run it manually (the `--repo NAME=PATH` form gives you the full
multi-repo allowlist that the provision script doesn't expose yet):

```bash
DEVICE_CONNECT_ALLOW_INSECURE=true \
TENANT=alice \
MESSAGING_BACKEND=zenoh \
ZENOH_LISTEN=tcp/0.0.0.0:7447 \
coding-worker \
  --device-id jetson-01 \
  --exec-cmd 'codex exec --full-auto {prompt}' \
  --repo shared-app=~/repos/shared-app \
  --repo infra=~/repos/infra \
  --default-repo shared-app
```

Dispatcher-side manual install:

```bash
pipx install 'device-connect-agent-tools[mcp]'
```

```toml
# ~/.codex/config.toml
[mcp_servers.device-connect]
command = "python"
args = ["-m", "device_connect_agent_tools.mcp"]
[mcp_servers.device-connect.env]
TENANT = "alice"
DEVICE_CONNECT_ALLOW_INSECURE = "true"
MESSAGING_BACKEND = "zenoh"
ZENOH_CONNECT = "tcp/10.0.0.11:7447"
DEVICE_CONNECT_DISCOVERY_MODE = "d2d"
```
