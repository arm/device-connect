# Walkthrough — agent-to-agent dispatch over Device Connect

End-to-end runtime trace of the coding-worker example. Every line below
is something the user actually sees on the terminal (server side) or the
worker logs (Pi side). No code is written by hand — the worker is the
generic CLI shipped in this directory; the dispatcher is whatever
MCP-speaking coding agent the user already runs (codex, claude-code, …).

---

## 0. One-time setup

### Server (dispatcher host)

```bash
pipx install 'device-connect-agent-tools[mcp]'
```

Add a stanza to the agent's MCP config. For codex CLI
(`~/.codex/config.toml`):

```toml
[mcp_servers.device-connect]
command = "python"
args = ["-m", "device_connect_agent_tools.mcp"]
env = { TENANT = "alice", DEVICE_CONNECT_ALLOW_INSECURE = "true" }
```

### Pi (worker host)

Provision the worker without `pip install` on the Pi — build a zipapp
on the dev box, scp it over:

```bash
# on dev machine, one-time
pip install shiv
shiv -c coding-worker -o coding-worker.pyz \
  ./examples/coding_worker device-connect-edge

# from the dispatcher host
scp coding-worker.pyz pi-desk:~/
```

That's it for setup. The Pi only needs Python 3.9+ (Raspbian ships it).

---

## 1. Start the worker on the Pi

```bash
pi@pi-desk:~ $ DEVICE_CONNECT_ALLOW_INSECURE=true \
    TENANT=alice \
    python3 coding-worker.pyz \
      --device-id pi-desk \
      --exec-cmd 'codex exec --full-auto {prompt}' \
      --repo shared-app=~/repos/shared-app \
      --repo infra=~/repos/infra \
      --default-repo shared-app

[coding-worker] connecting to messaging fabric (zenoh, D2D)
[coding-worker] tenant=alice device_id=pi-desk
[coding-worker] repos: shared-app=/home/pi/repos/shared-app, infra=/home/pi/repos/infra
[coding-worker] default repo: shared-app
[coding-worker] announcing presence on device-connect.alice.pi-desk.presence
[coding-worker] ready — waiting for dispatches
```

Run this under systemd or a screen/tmux session so it stays up.

---

## 2. Start the agent on the server

```bash
user@server:~/shared-app $ codex
```

```
codex › MCP servers loaded: device-connect (4 tools)
codex › Available tools: describe_fleet, list_devices,
        get_device_functions, invoke_device
codex › How can I help?
```

---

## 3. Dispatch a task

```
❯ There's a flaky test in tests/auth/test_login.py — can you hand
  it off to the worker on my desk to investigate while I keep
  working on the dashboard branch?
```

The agent picks tools by itself. Internally:

```
codex › I'll delegate this to a coding worker. Let me find one.

[tool] describe_fleet()
  → {"total_devices": 1,
     "by_type": {"coding-worker": {"count": 1}},
     "devices": [{
       "device_id": "pi-desk",
       "device_type": "coding-worker",
       "functions": [
         {"name": "list_repos", "params": {}},
         {"name": "dispatch",   "params": {
            "prompt": "string", "base_ref": "string",
            "task_id": "string", "repo": "string"}},
         {"name": "task_status", "params": {"task_id": "string"}},
         {"name": "cancel",     "params": {"task_id": "string"}}
       ],
       "events": ["progress", "work_done", "work_failed"]
     }]}

codex › I see one worker, pi-desk. Checking which repo to target.

[tool] invoke_device(pi-desk, "list_repos")
  → {"repos": [
       {"name": "shared-app", "path": "/home/pi/repos/shared-app"},
       {"name": "infra",      "path": "/home/pi/repos/infra"}],
     "default": "shared-app"}

codex › "shared-app" matches the test path you mentioned.
        Dispatching the task.

[tool] invoke_device(
  device_id="pi-desk",
  function="dispatch",
  params={
    "task_id": "T-42",
    "repo":    "shared-app",
    "base_ref": "main",
    "prompt":  "Investigate flaky test at tests/auth/test_login.py.
                Reproduce, root-cause it, propose a fix on a feature
                branch, and push."
  },
  llm_reasoning="user asked to delegate flaky-test investigation"
)
  → {"success": true,
     "result": {"accepted": true, "task_id": "T-42", "repo": "shared-app"}}

codex › Dispatched as task T-42 on pi-desk (repo: shared-app).
        I'll watch for the completion event and let you know
        when the branch is pushed. Keep working — I'll interrupt.
❯ ▏
```

The user is unblocked. Codex stays subscribed to events in the background.

---

## 4. Meanwhile on the Pi

```
[coding-worker] T-42 accepted: repo=shared-app
[coding-worker] T-42 emit progress {"step": "checkout", "detail": "shared-app@main"}
[coding-worker] T-42 git fetch origin main
[coding-worker] T-42 git checkout -B feature/T-42 origin/main
[coding-worker] T-42 emit progress {"step": "agent"}
[coding-worker] T-42 spawning: codex exec --full-auto 'Investigate flaky...'
[coding-worker] T-42 (codex) reading tests/auth/test_login.py
[coding-worker] T-42 (codex) reproducing flake locally — 12/50 fails
[coding-worker] T-42 (codex) hypothesis: race in session teardown
[coding-worker] T-42 (codex) applying fix to auth/session.py
[coding-worker] T-42 (codex) re-running 200x — all pass
[coding-worker] T-42 emit progress {"step": "commit"}
[coding-worker] T-42 git add -A
[coding-worker] T-42 git commit -m "T-42: Investigate flaky test at tests/..."
[coding-worker] T-42 emit progress {"step": "push", "detail": "feature/T-42"}
[coding-worker] T-42 git push -u origin feature/T-42
[coding-worker] T-42 done — sha=a3f91c2 emit work_done
```

---

## 5. The event arrives on the server

### How event delivery really works through MCP today

Device Connect itself has a subscribe API (`conn.subscribe_events(...)`),
and the worker's `work_done` is a real publish on
`device-connect.alice.pi-desk.event.work_done`. That part is solid.

But the path from that event to a codex CLI session depends on the MCP
bridge. The bridge registered with codex today exposes four tools
(`describe_fleet`, `list_devices`, `get_device_functions`, `invoke_device`)
and **no event-subscription tool**. That has two consequences worth
being explicit about:

1. **Codex does not get spontaneously interrupted.** There is no
   "background push" into an idle chat session without an upstream
   bridge change (see §6 below).
2. **The bridge buffers events server-side regardless.** When codex
   does call a device-side RPC or a future `pending_events` tool, it
   can retrieve what happened in the meantime.

So the accurate experience today is **pull, not push** — pleasantly
pullable because the worker emits structured events that get buffered.

### What the user actually sees

A few minutes after dispatching, the user comes back to the agent:

```
❯ any updates from pi-desk?

codex › Let me check.
[tool] invoke_device(pi-desk, "task_status", {"task_id": "T-42"})
  → {"state": "done",
     "repo": "shared-app",
     "branch": "feature/T-42",
     "sha": "a3f91c2",
     "summary": "Fixed race in session teardown; added retry guard."}

codex › T-42 finished — feature/T-42 pushed (a3f91c2). Want me
        to fetch it and walk through the diff?

❯ yes

codex › [running] git fetch origin feature/T-42
codex › [running] git log --oneline main..feature/T-42
   a3f91c2 fix(auth): guard against race in session teardown
   8e1dd4f test(auth): stability harness for test_login

codex › Here's the relevant hunk in src/auth/session.py:
   ─────────────────────────────────────────────
   @@ -41,6 +41,11 @@ class Session:
        def teardown(self):
            self._db.close()
   +        # Guard against early teardown racing with
   +        # in-flight refresh tokens.
   +        with self._refresh_lock:
   +            if self._pending_refresh:
   +                self._pending_refresh.cancel()
            self._cookie.clear()
   ─────────────────────────────────────────────

❯ looks good, merge it
```

### Three delivery shapes (the bigger picture)

| Pattern | What it does | Reality today |
| --- | --- | --- |
| **Pull** | Dispatcher calls `task_status(task_id)` when asked | Works on every MCP client. The default in this walkthrough. |
| **Long-poll** | New bridge tool `wait_for_event(...)` blocks until an event arrives, stream via `notifications/progress` | Works everywhere once the bridge adds the tool. Parks one MCP slot for up to the timeout — dispatcher is busy during that window. |
| **Resource push** | Bridge exposes `events://{device_id}/latest`, sends `notifications/resources/updated` | Non-blocking, truly push, but client support as of early 2026 is uneven. Best on Claude Desktop. |

None of this is a limitation of Device Connect — it's a gap in what
the MCP bridge wraps. Adding `wait_for_event` is a ~60-line change to
`packages/device-connect-agent-tools/device_connect_agent_tools/mcp/bridge.py`;
resource-push is a bigger change but well-defined. Both sit above the
Device Connect fabric, not inside it.

### Non-MCP dispatcher gets push for free

If the dispatcher is a Python process using
`device_connect_agent_tools` directly (Strands / LangChain / Claude
SDK / a plain script), `conn.subscribe_events(device_id="pi-desk")`
yields event batches live. The walkthrough's "agent interrupts you
mid-sentence" UX is genuine there — it's the MCP bridge specifically
that's pull-only today.

---

## 6. The failure path — how the server learns what went wrong

This is the concern that matters in practice: you dispatched a task,
something broke on the Pi, and the dispatcher codex needs enough
information to decide between retry, escalate, or ask you. The worker
gives you that in three layers.

### Layer 1 — the `work_failed` event carries structured fields

```
invoke_device(pi-desk, "task_status", {"task_id": "T-43"})
  → {
      "state":    "failed",
      "category": "rate_limit",
      "step":     "agent",
      "repo":     "shared-app",
      "error":    "agent exited 1: rate limited by upstream API"
    }
```

The same payload is in the `work_failed` emit. Fixed vocabulary:

- `precondition`  — repo / base ref / config wrong before work started
- `agent_error`   — coding CLI exited non-zero
- `no_changes`    — agent produced zero diff
- `conflict`      — git operation failed (push rejected, merge conflict)
- `auth`          — credentials / permission
- `rate_limit`    — upstream API throttled
- `cancelled`     — dispatcher asked to cancel
- `unknown`       — uncaught — check logs

### Layer 2 — `log_tail` in the event

The event also carries the last 20 lines of that task's combined
output. Enough for the dispatcher to decide without a second round-trip
in most cases:

```
"log_tail": [
  "+ claude -p 'Investigate flaky test...'",
  "claude: error: 429 Too Many Requests",
  "claude: try again in 38s",
  "+ exit 1"
]
```

### Layer 3 — `get_logs` RPC for the full picture

When the tail isn't enough, the dispatcher asks for more:

```
invoke_device(pi-desk, "get_logs", {"task_id": "T-43", "tail": 500})
  → {"lines": [...], "truncated": true, "total": 1247}
```

Same RPC works on a running task — you can peek at what the Pi is
doing right now:

```
❯ the task's been running 8 minutes with no event — what's happening?

codex › [tool] invoke_device(pi-desk, "get_logs",
                             {"task_id": "T-42", "tail": 100})
  → {"lines": [
       "codex: re-running test suite iteration 42/200",
       "...all pass...",
       "codex: confidence check — running 50 more iterations"
     ], ...}

codex › Still iterating on stability. No failures yet. Want me to
        wait, or should I tell it to wrap up?
```

### How the dispatcher agent should react by category

| Category | Sensible default action |
| --- | --- |
| `no_changes` | Surface the log tail and ask the user whether to re-prompt |
| `rate_limit` | Wait N seconds and re-dispatch to the same worker |
| `conflict` | Server-side rebase on fresh `main`, re-dispatch, or escalate if persistent |
| `auth` | Stop — surface to user. The Pi needs credentials refreshed (git remote, API key, beads server) |
| `precondition` | Check worker config; `list_repos()` to verify repo names, retry with corrected params |
| `agent_error` | Pull the log tail, show the user, offer retry or bail |
| `cancelled` | No action |
| `unknown` | Pull `get_logs`, paste relevant excerpt to the user, ask |

You don't need to program any of that into codex — you put it in the
agent's system prompt or `AGENTS.md` and it follows the recipe. The
structured fields are what make that possible; without them the agent
is reduced to regex-matching an error string.

### What the worker deliberately does *not* do

- **It does not retry on its own.** Retry is a policy decision for the
  dispatcher, which knows whether the task is idempotent, whether the
  user is watching, and whether beads still has the task claimable.
- **It does not leave half-built branches on the remote.** If `push`
  fails, nothing was pushed. If push succeeded but `work_done` never
  fired (worker crash mid-task), the branch is on the remote and the
  `status()` RPC will report `"state": "unknown"` — dispatcher can
  recover by `git fetch` and inspecting.
- **It does not ask the user directly.** Two-way mid-task dialog
  ("please give me your API key") would require a new RPC pair
  (`need_input` / `resume`). For now the worker fails fast with
  `category="auth"` and the dispatcher surfaces it.

---

## 7. What never happened

- The user never typed `ssh pi-desk`.
- The user never ran `scp` or wrote a wrapper script.
- The user never typed `pi-desk.local` or an IP address — codex found
  the worker by capability (`device_type="coding-worker"`).
- The user never greped a log file on the Pi — the last 20 lines of
  relevant output came with `work_failed`, and `get_logs` pulls more.
- The dispatcher CLI never blocked on a long SSH session. Work ran on
  the Pi; the user checked back when they were ready.

---

## 8. Multi-tenant: what alice's friend bob sees

bob has the same setup, but every component is launched with
`TENANT=bob`. On the wire, alice publishes to `device-connect.alice.*`
and bob to `device-connect.bob.*`. When bob runs `describe_fleet()`:

```
codex › [tool] describe_fleet()
  → {"total_devices": 0, "by_type": {}, "by_location": {}}
```

bob does not see alice's `pi-desk` and vice versa. (Caveat: with
`DEVICE_CONNECT_ALLOW_INSECURE=true` this is a logical separation, not
a cryptographic one — see the "Multi-tenant isolation" section in
[`README.md`](README.md) for Tier 2 / Tier 3 hardening.)

---

## 9. Mental model summary

```
User types in codex on server
        │
        ▼
codex picks the device-connect MCP tools
        │
        ▼
invoke_device  ──►  [Device Connect fabric]  ──►  Pi worker driver
                                                       │
                                                       ▼
                                                  codex subprocess
                                                       │
                                                  git push branch
                                                       │
                                                       ▼
work_done event  ◄──  [Device Connect fabric]  ◄──  emit
        │
        ▼
codex surfaces the branch + diff in the user's session
```

Three things to think about before scaling this beyond a personal setup:

1. **Authorization.** First time codex calls `invoke_device`, the MCP
   client may prompt for approval. Decide if you want to auto-approve
   per-server or per-tool — annoying every time, risky once.
2. **Logging correlation.** The bridge already attaches `llm_reasoning`
   and a `trace_id`. The worker echoes both into its logs so you can
   match a server-side prompt to a Pi-side subprocess.
3. **Concurrency on the same repo.** Two parallel tasks targeting the
   same repo will conflict on `git checkout`. Either serialize per
   repo (one task at a time) or extend `_run()` to use
   `git worktree add`. See the README for the worktree sketch.
