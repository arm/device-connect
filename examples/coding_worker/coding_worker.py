"""Generic coding-agent worker driver.

Exposes an RPC surface (dispatch / status / cancel) and emits events
(progress / work_done / work_failed) so a dispatcher agent running
elsewhere on the Device Connect mesh can hand off a prompt, let the
worker run a local coding CLI (codex, claude, aider, …), push the
resulting feature branch, and be notified when it's done.

Not codex-specific — the coding CLI is wired via `--exec-cmd`.

Run (single repo):
    DEVICE_CONNECT_ALLOW_INSECURE=true \\
    TENANT=alice \\
    python coding_worker.py \\
      --device-id pi-desk \\
      --exec-cmd 'codex exec --full-auto {prompt}' \\
      --repo-path ~/repos/shared-app

Run (multiple repos — caller picks one by name):
    python coding_worker.py \\
      --device-id pi-desk \\
      --exec-cmd 'codex exec --full-auto {prompt}' \\
      --repo shared-app=~/repos/shared-app \\
      --repo infra=~/repos/infra \\
      --default-repo shared-app
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import uuid
from pathlib import Path

from device_connect_edge import DeviceRuntime
from device_connect_edge.drivers import DeviceDriver, rpc, emit


class CodingWorkerDriver(DeviceDriver):
    device_type = "coding-worker"

    def __init__(
        self,
        exec_cmd: str,
        repos: dict[str, Path],
        default_repo: str | None = None,
    ) -> None:
        super().__init__()
        if not repos:
            raise ValueError("At least one repo must be configured")
        self._exec_cmd = exec_cmd
        self._repos = repos
        # Default repo resolution: explicit pick → only entry → first entry
        if default_repo is not None:
            if default_repo not in repos:
                raise ValueError(f"--default-repo '{default_repo}' not in repo allowlist")
            self._default_repo = default_repo
        elif len(repos) == 1:
            self._default_repo = next(iter(repos))
        else:
            self._default_repo = next(iter(repos))
        self._tasks: dict[str, asyncio.Task] = {}
        self._results: dict[str, dict] = {}

    @rpc()
    async def list_repos(self) -> dict:
        """List the repos this worker is allowed to run tasks in."""
        return {
            "repos": [
                {"name": name, "path": str(path)} for name, path in self._repos.items()
            ],
            "default": self._default_repo,
        }

    @rpc()
    async def dispatch(
        self,
        prompt: str,
        base_ref: str = "main",
        task_id: str = "",
        repo: str = "",
    ) -> dict:
        """Start a coding task on this worker and return immediately.

        Args:
            prompt: Natural-language instruction for the coding agent.
            base_ref: Git ref to branch from (default: main).
            task_id: Caller-supplied id. If empty, one is generated.
            repo: Repo name from list_repos(). If empty, the default is used.
        """
        repo_name = repo or self._default_repo
        repo_path = self._repos.get(repo_name)
        if repo_path is None:
            return {
                "accepted": False,
                "reason": f"unknown repo '{repo_name}' — call list_repos() for the allowlist",
            }

        task_id = task_id or f"T-{uuid.uuid4().hex[:6]}"
        existing = self._tasks.get(task_id)
        if existing is not None and not existing.done():
            return {"accepted": False, "reason": "task already running", "task_id": task_id}
        self._tasks[task_id] = asyncio.create_task(
            self._run(task_id, prompt, base_ref, repo_path, repo_name),
        )
        return {"accepted": True, "task_id": task_id, "repo": repo_name}

    @rpc()
    async def status(self, task_id: str) -> dict:
        """Query task state (running / done / failed / unknown).

        Args:
            task_id: Task id returned by dispatch().
        """
        task = self._tasks.get(task_id)
        if task is None:
            return {"state": "unknown"}
        if not task.done():
            return {"state": "running"}
        result = self._results.get(task_id, {})
        return {"state": "failed" if "error" in result else "done", **result}

    @rpc()
    async def cancel(self, task_id: str) -> dict:
        """Cancel a running task.

        Args:
            task_id: Task id returned by dispatch().
        """
        task = self._tasks.get(task_id)
        if task is None:
            return {"cancelled": False, "reason": "unknown task"}
        if task.done():
            return {"cancelled": False, "reason": "already finished"}
        task.cancel()
        return {"cancelled": True}

    @emit()
    async def progress(self, task_id: str, step: str, detail: str = "") -> dict:
        """Streaming progress update."""

    @emit()
    async def work_done(self, task_id: str, branch: str, sha: str, summary: str) -> dict:
        """Task finished; feature branch was pushed."""

    @emit()
    async def work_failed(self, task_id: str, error: str) -> dict:
        """Task failed; no branch was pushed."""

    async def _run(
        self, task_id: str, prompt: str, base_ref: str, repo_path: Path, repo_name: str,
    ) -> None:
        branch = f"feature/{task_id}"
        try:
            await self.progress(task_id=task_id, step="checkout", detail=f"{repo_name}@{base_ref}")
            await self._git(repo_path, "fetch", "origin", base_ref)
            await self._git(repo_path, "checkout", "-B", branch, f"origin/{base_ref}")

            await self.progress(task_id=task_id, step="agent")
            cmd = self._exec_cmd.format(prompt=shlex.quote(prompt))
            rc, stdout, stderr = await self._shell(cmd, cwd=repo_path)
            if rc != 0:
                raise RuntimeError(f"agent exited {rc}: {stderr[-400:].strip()}")

            rc, _, _ = await self._shell(
                "git diff --quiet && git diff --cached --quiet", cwd=repo_path,
            )
            if rc == 0:
                raise RuntimeError("agent produced no changes")

            await self.progress(task_id=task_id, step="commit")
            await self._git(repo_path, "add", "-A")
            await self._git(repo_path, "commit", "-m", f"{task_id}: {prompt[:60]}")

            await self.progress(task_id=task_id, step="push", detail=branch)
            await self._git(repo_path, "push", "-u", "origin", branch)

            sha = (await self._capture(repo_path, "git", "rev-parse", "HEAD")).strip()
            summary = (stdout.strip().splitlines() or ["done"])[-1][:400]
            self._results[task_id] = {
                "repo": repo_name, "branch": branch, "sha": sha, "summary": summary,
            }
            await self.work_done(task_id=task_id, branch=branch, sha=sha, summary=summary)

        except asyncio.CancelledError:
            self._results[task_id] = {"error": "cancelled", "repo": repo_name}
            await self.work_failed(task_id=task_id, error="cancelled")
            raise
        except Exception as exc:
            self._results[task_id] = {"error": str(exc), "repo": repo_name}
            await self.work_failed(task_id=task_id, error=str(exc))

    async def _git(self, cwd: Path, *args: str) -> None:
        rc, _, stderr = await self._shell(
            "git " + " ".join(shlex.quote(a) for a in args), cwd=cwd,
        )
        if rc != 0:
            raise RuntimeError(f"git {args[0]} failed: {stderr.strip()}")

    async def _capture(self, cwd: Path, *args: str) -> str:
        rc, stdout, stderr = await self._shell(
            " ".join(shlex.quote(a) for a in args), cwd=cwd,
        )
        if rc != 0:
            raise RuntimeError(f"{args[0]} failed: {stderr.strip()}")
        return stdout

    async def _shell(self, cmd: str, cwd: Path) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def _parse_repo_flag(raw: str) -> tuple[str, Path]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(
            f"--repo expects NAME=PATH, got {raw!r}",
        )
    name, _, path = raw.partition("=")
    name, path = name.strip(), path.strip()
    if not name or not path:
        raise argparse.ArgumentTypeError(f"--repo NAME and PATH must be non-empty: {raw!r}")
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_dir():
        raise argparse.ArgumentTypeError(f"--repo path does not exist: {resolved}")
    return name, resolved


def main() -> None:
    parser = argparse.ArgumentParser(prog="coding-worker")
    parser.add_argument("--device-id", required=True)
    parser.add_argument(
        "--exec-cmd",
        required=True,
        help=(
            "Shell command template. Use {prompt} where the caller's "
            "prompt should be injected — the worker shell-quotes it, so "
            "do NOT wrap {prompt} in your own quotes. "
            "Example: 'codex exec --full-auto {prompt}'"
        ),
    )
    parser.add_argument(
        "--repo",
        action="append",
        type=_parse_repo_flag,
        default=[],
        metavar="NAME=PATH",
        help=(
            "Allowed repo entry, repeatable. Callers pick one by NAME via the "
            "dispatch(repo=...) param. Paths are resolved at startup and the "
            "worker refuses to run outside this allowlist."
        ),
    )
    parser.add_argument(
        "--repo-path",
        help=(
            "Shortcut for a single-repo setup. Equivalent to "
            "--repo default=PATH. Ignored if --repo is used."
        ),
    )
    parser.add_argument(
        "--default-repo",
        help="Repo name to use when the caller omits `repo`. Defaults to the first --repo entry.",
    )
    parser.add_argument(
        "--tenant",
        default=os.environ.get("TENANT", "default"),
        help="Tenant namespace (default: $TENANT or 'default').",
    )
    args = parser.parse_args()

    repos: dict[str, Path] = dict(args.repo)
    if not repos and args.repo_path:
        name, path = _parse_repo_flag(f"default={args.repo_path}")
        repos[name] = path
    if not repos:
        parser.error("must pass --repo NAME=PATH (repeatable) or --repo-path PATH")

    driver = CodingWorkerDriver(
        exec_cmd=args.exec_cmd,
        repos=repos,
        default_repo=args.default_repo,
    )
    runtime = DeviceRuntime(driver=driver, device_id=args.device_id, tenant=args.tenant)
    asyncio.run(runtime.run())


if __name__ == "__main__":
    main()
