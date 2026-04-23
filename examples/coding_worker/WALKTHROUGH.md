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
         {"name": "status",     "params": {"task_id": "string"}},
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

The user is mid-sentence on something else when the agent interrupts:

```
❯ meanwhile let me refactor the navbar compo─

[event] pi-desk::work_done {
  "task_id": "T-42",
  "branch": "feature/T-42",
  "sha":    "a3f91c2",
  "summary": "Fixed race in session teardown; added retry guard.
              Test now stable across 200 runs."
}

codex › Task T-42 finished on pi-desk. Branch feature/T-42 was pushed
        — want me to fetch it and walk you through the diff, or keep
        going with the navbar?

❯ fetch it and show me the diff
```

```
codex › [running] git fetch origin feature/T-42
codex › [running] git log --oneline main..feature/T-42
   a3f91c2 fix(auth): guard against race in session teardown
   8e1dd4f test(auth): stability harness for test_login

codex › Here's the relevant hunk in src/auth/session.py:
   ─────────────────────────────────────────────
   @@ -41,6 +41,11 @@ class Session:
        def teardown(self):
            self._db.close()
   +        # T-42 fix: guard against early teardown racing with
   +        # in-flight refresh tokens.
   +        with self._refresh_lock:
   +            if self._pending_refresh:
   +                self._pending_refresh.cancel()
            self._cookie.clear()
   ─────────────────────────────────────────────

❯ looks good, merge it
```

---

## 6. The failure path

If codex on the Pi crashes, hits a sandbox limit, or produces no diff,
the worker emits `work_failed` instead and the dispatcher hears about
it the same way:

```
[event] pi-desk::work_failed {
  "task_id": "T-43",
  "error":   "agent exited 1: rate limited by upstream API"
}

codex › Task T-43 failed on pi-desk: "rate limited by upstream API".
        Want me to retry in a few minutes, or send it to a different
        worker?
```

---

## 7. What never happened

- The user never typed `ssh pi-desk`.
- The user never ran `scp` or wrote a wrapper script.
- The user never typed `pi-desk.local` or an IP address — codex found
  the worker by capability (`device_type="coding-worker"`).
- The user never polled a log file or refreshed `git fetch` to know if
  the Pi was done — the `work_done` event woke them up.
- The dispatcher CLI did not block. Long-running work happened on the
  Pi while the user did other things.

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
