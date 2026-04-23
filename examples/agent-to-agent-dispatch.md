# Agent-to-Agent Dispatch over Device Connect

This example walks through a pattern where one AI coding agent (running on a
server) dispatches work to another AI coding agent (running on a remote host
such as a Raspberry Pi or workstation) and receives asynchronous completion
signals — all over a Device Connect mesh.

The "work product" is a Git branch: the worker pushes a feature branch to a
shared remote, then emits a `work_done` event carrying the branch name and
commit SHA. The dispatcher fetches the branch and continues from there.

## Scenario

- **Server**: runs a coding agent acting as the dispatcher. Has
  `device-connect-agent-tools` installed.
- **Worker host**: runs a coding agent acting as the executor. Has
  `device-connect-edge` installed and exposes a driver with `@rpc` methods and
  `@emit` events.
- **Shared Git remote**: both hosts can push/fetch from the same repository.
- **Network**: any network on which Device Connect peers can reach each other
  (local LAN with Zenoh multicast, or a NATS/MQTT broker reachable by both).

## Architecture

```
                              Device Connect mesh
 ┌──────────────────────────────────────────────────────────────────────────┐
 │                                                                          │
 │   ┌─────────────────────────────────┐        ┌────────────────────────┐  │
 │   │  SERVER  (dispatcher host)      │        │  WORKER  (executor)    │  │
 │   │                                 │        │                        │  │
 │   │  ┌───────────────────────────┐  │        │  ┌──────────────────┐  │  │
 │   │  │ coding agent (dispatcher) │  │        │  │ coding agent     │  │  │
 │   │  └──────────────┬────────────┘  │        │  │  (worker shell)  │  │  │
 │   │                 │ tool calls    │        │  └────────▲─────────┘  │  │
 │   │                 ▼               │        │           │ subprocess │  │
 │   │  ┌───────────────────────────┐  │        │  ┌────────┴─────────┐  │  │
 │   │  │ device-connect-agent-tools│  │        │  │ WorkerDriver     │  │  │
 │   │  │  describe_fleet()         │  │        │  │  @rpc dispatch   │  │  │
 │   │  │  invoke_device(...)       │  │        │  │  @rpc status     │  │  │
 │   │  │  subscribe_events(...)    │  │        │  │  @emit work_done │  │  │
 │   │  └──────────────┬────────────┘  │        │  └────────┬─────────┘  │  │
 │   │                 │               │        │           │            │  │
 │   │  ┌──────────────┴────────────┐  │        │  ┌────────┴─────────┐  │  │
 │   │  │ DeviceConnection          │  │        │  │ DeviceRuntime    │  │  │
 │   │  │  (NATS | Zenoh | MQTT)    │  │        │  │  + Announcer     │  │  │
 │   │  └──────────────┬────────────┘  │        │  └────────┬─────────┘  │  │
 │   └─────────────────┼───────────────┘        └───────────┼────────────┘  │
 │                     │                                    │               │
 │                     │         pub/sub fabric             │               │
 │                     └──────────────► ◄───────────────────┘               │
 │                                                                          │
 └──────────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
                         ┌───────────────────────────┐
                         │   Shared Git remote       │
                         │   (feature/* branches)    │
                         └───────────────────────────┘
```

## Message flow for one task

```
  Server agent         agent-tools            pub/sub           Worker Driver      Worker agent        Git remote
      │                      │                   │                    │                  │                  │
      │ "run task T"         │                   │                    │                  │                  │
      ├─────────────────────►│                   │                    │                  │                  │
      │                      │ discovery.probe   │                    │                  │                  │
      │                      ├──────────────────►│                    │                  │                  │
      │                      │                   │  presence          │                  │                  │
      │                      │◄──────────────────┤◄───────────────────┤                  │                  │
      │                      │                   │                    │                  │                  │
      │ pick "worker-01"     │                   │                    │                  │                  │
      │                      │ invoke(dispatch)  │                    │                  │                  │
      │                      │   JSON-RPC req    │                    │                  │                  │
      │                      ├──────────────────►│                    │                  │                  │
      │                      │ device-connect.   │                    │                  │                  │
      │                      │ default.worker-01 │                    │                  │                  │
      │                      │ .cmd              ├───────────────────►│                  │                  │
      │                      │                   │                    │ exec agent(task) │                  │
      │                      │                   │                    ├─────────────────►│                  │
      │                      │                   │                    │                  │                  │
      │                      │  ack {accepted}   │                    │◄─────────────────┤ (running…)       │
      │                      │◄──────────────────┤◄───────────────────┤                  │                  │
      │ "accepted, id=T-42"  │                   │                    │                  │                  │
      │◄─────────────────────┤                   │                    │                  │                  │
      │                      │                   │                    │                  │                  │
      │ subscribe_events(    │                   │                    │                  │ git push origin  │
      │   device=worker-01)  │                   │                    │                  │ feature/T-42     │
      │                      │                   │                    │                  ├─────────────────►│
      │                      │                   │                    │                  │                  │
      │                      │                   │                    │ @emit work_done  │                  │
      │                      │                   │                    │  {branch, sha,   │                  │
      │                      │                   │◄───────────────────┤   summary}       │                  │
      │                      │  event delivered  │                    │                  │                  │
      │                      │◄──────────────────┤                    │                  │                  │
      │ event: work_done     │                   │                    │                  │                  │
      │◄─────────────────────┤                   │                    │                  │                  │
      │                      │                   │                    │                  │                  │
      │ git fetch &          │                   │                    │                  │                  │
      │ checkout feature/T-42│                   │                    │                  │                  │
      ├───────────────────────────────────────────────────────────────────────────────────────────────────►│
      │ review / iterate     │                   │                    │                  │                  │
      ▼                      ▼                   ▼                    ▼                  ▼                  ▼
```

## Subjects on the wire

```
  device-connect.default.discovery.probe              ← probe, any peer → bus
  device-connect.default.worker-01.presence           ← heartbeat,  worker → bus
  device-connect.default.worker-01.cmd                ← JSON-RPC,   server → worker
  device-connect.default.worker-01.event.work_done    ← event emit, worker → bus
  device-connect.default.worker-01.event.progress     ← optional progress stream
```

## Why use Device Connect for this instead of SSH + scripts

| Concern | SSH + scripts | Device Connect |
| --- | --- | --- |
| Agent integration | wrap `ssh host 'cmd'` in a tool, parse stdout | typed `@rpc` surface consumed as a structured agent tool |
| Completion signaling | block on long command, poll, or build a webhook | `@emit` event + `subscribe_events` on the dispatcher |
| Discovery | hard-coded hostnames | `discover_devices(device_type=...)` by capability |
| Adding a second worker | edit the dispatcher script | new worker joins the pool automatically |
| Authz per action | all-or-nothing via SSH keys | per-device ACLs on the RPC surface |
| Errors | exit codes + stderr parsing | JSON-RPC error objects with structured fields |

When the flow is truly one-shot and single-host, SSH is fine. The Device
Connect win grows with agent integration depth, async completion, and fleet
size.

## Worker driver skeleton

```python
# worker_driver.py — runs on the worker host
import asyncio
import subprocess
from device_connect_edge import DeviceRuntime
from device_connect_edge.drivers import DeviceDriver, rpc, emit


class WorkerDriver(DeviceDriver):
    device_type = "coding-worker"

    def __init__(self) -> None:
        super().__init__()
        self._tasks: dict[str, asyncio.Task] = {}

    @rpc()
    async def dispatch(self, task_id: str, prompt: str, repo_ref: str) -> dict:
        """Start a task in the background; return immediately with an ack."""
        if task_id in self._tasks:
            return {"accepted": False, "reason": "task already running"}

        self._tasks[task_id] = asyncio.create_task(self._run(task_id, prompt, repo_ref))
        return {"accepted": True, "task_id": task_id}

    @rpc()
    async def status(self, task_id: str) -> dict:
        task = self._tasks.get(task_id)
        if task is None:
            return {"state": "unknown"}
        if task.done():
            return {"state": "done"}
        return {"state": "running"}

    @emit()
    async def work_done(self, task_id: str, branch: str, sha: str, summary: str) -> dict:
        """Emitted when a task finishes and its branch has been pushed."""
        return {"task_id": task_id, "branch": branch, "sha": sha, "summary": summary}

    async def _run(self, task_id: str, prompt: str, repo_ref: str) -> None:
        branch = f"feature/{task_id}"
        # 1. check out repo_ref, create branch
        # 2. invoke local coding agent with the prompt
        # 3. commit + push the feature branch
        # (error handling, cancellation, streaming progress events omitted)
        proc = await asyncio.create_subprocess_exec(
            "scripts/run_task.sh", task_id, prompt, repo_ref, branch,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        sha = stdout.decode().strip()

        await self.work_done(task_id=task_id, branch=branch, sha=sha, summary="ok")


asyncio.run(DeviceRuntime(driver=WorkerDriver(), device_id="worker-01").run())
```

## Dispatcher agent skeleton

```python
# dispatcher.py — runs on the server, alongside the dispatcher agent
import asyncio
from device_connect_agent_tools import connect, list_devices, invoke_device
from device_connect_agent_tools.connection import get_connection


def dispatch_and_wait(task_id: str, prompt: str, repo_ref: str) -> dict:
    connect()

    workers = list_devices(device_type="coding-worker", status="online")["devices"]
    if not workers:
        raise RuntimeError("no coding workers available")
    worker_id = workers[0]["device_id"]

    ack = invoke_device(
        device_id=worker_id,
        function="dispatch",
        params={"task_id": task_id, "prompt": prompt, "repo_ref": repo_ref},
        llm_reasoning=f"dispatching task {task_id} to worker {worker_id}",
    )
    if not ack.get("success"):
        raise RuntimeError(f"dispatch rejected: {ack}")

    return asyncio.run(_wait_for_done(worker_id, task_id))


async def _wait_for_done(worker_id: str, task_id: str) -> dict:
    conn = get_connection()
    async for batch in conn.subscribe_events(batch_window=1.0, device_id=worker_id):
        for event in batch:
            if event["event_name"] == "work_done" and event["params"].get("task_id") == task_id:
                return event["params"]
```

## How the agent tool layer wires up

- `connect()` builds a singleton `DeviceConnection` — backend auto-detected
  from env (`MESSAGING_BACKEND`, `ZENOH_CONNECT`, `NATS_URL`, …) and
  credentials auto-discovered from `security_infra/credentials/` when
  running inside a Device Connect project tree.
  See `packages/device-connect-agent-tools/device_connect_agent_tools/connection.py`.
- Discovery uses a `PresenceCollector` (D2D mode) or the server-backed
  registry client, exposed through the same `DiscoveryProvider` interface.
- `invoke_device(...)` turns into a JSON-RPC request/reply on
  `device-connect.{zone}.{device_id}.cmd`.
- `subscribe_events(...)` subscribes to
  `device-connect.{zone}.{device_id}.event.>` and yields parsed event batches.

## Adapter choices for the dispatcher agent

The same four primitives — `describe_fleet`, `list_devices`,
`get_device_functions`, `invoke_device` — are exposed as native tools for
several agent frameworks:

- Strands: `device_connect_agent_tools.adapters.strands`
- LangChain: `device_connect_agent_tools.adapters.langchain`
- Claude Agent SDK: `device_connect_agent_tools.adapters.claude`
- MCP: `device_connect_agent_tools.mcp`

Pick whichever matches the framework the dispatcher agent runs in — the
underlying `DeviceConnection` is shared.
