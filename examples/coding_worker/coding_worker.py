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


class _Fail(Exception):
    """Internal exception carrying a structured failure classification."""

    def __init__(self, category: str, message: str, detail: str = "") -> None:
        super().__init__(message if not detail else f"{message}: {detail}")
        self.category = category
        self.message = str(self)


def _classify_git_failure(git_subcmd: str, output: str) -> str:
    """Bucket a failed git command into a small fixed vocabulary."""
    lo = output.lower()
    if any(s in lo for s in ("authentication failed", "permission denied", "could not read username")):
        return "auth"
    if any(s in lo for s in (
        "non-fast-forward", "rejected", "conflict", "merge conflict",
        "would be overwritten", "cannot lock ref",
    )):
        return "conflict"
    if git_subcmd in ("fetch", "checkout"):
        return "precondition"
    return "unknown"


def _classify_exception(exc: Exception, step: str) -> str:
    msg = str(exc).lower()
    if "rate limit" in msg or "429" in msg or "too many requests" in msg:
        return "rate_limit"
    if step in ("checkout",):
        return "precondition"
    return "unknown"


def _read_log_tail(path: Path, lines: int) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(errors="replace").splitlines()[-lines:]
    except OSError:
        return []


class CodingWorkerDriver(DeviceDriver):
    device_type = "coding-worker"

    def __init__(
        self,
        exec_cmd: str,
        repos: dict[str, Path],
        default_repo: str | None = None,
        log_dir: Path | None = None,
        no_push: bool = False,
    ) -> None:
        super().__init__()
        if not repos:
            raise ValueError("At least one repo must be configured")
        self._exec_cmd = exec_cmd
        self._repos = repos
        self._no_push = no_push
        # Default repo resolution: explicit pick → only entry → first entry
        if default_repo is not None:
            if default_repo not in repos:
                raise ValueError(f"--default-repo '{default_repo}' not in repo allowlist")
            self._default_repo = default_repo
        else:
            self._default_repo = next(iter(repos))
        self._log_dir = log_dir or (
            Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
            / "coding-worker"
            / "logs"
        )
        self._log_dir.mkdir(parents=True, exist_ok=True)
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
    async def task_status(self, task_id: str) -> dict:
        """Query task state (running / done / failed / unknown).

        Named task_status (not just `status`) because DeviceDriver reserves
        `.status` for the DeviceStatus property.

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

    @rpc()
    async def get_logs(self, task_id: str, tail: int = 200) -> dict:
        """Return the last N lines of a task's combined stdout/stderr log.

        Useful for peeking at a running task or pulling more context after a
        work_failed event where the embedded log_tail isn't enough.

        Args:
            task_id: Task id returned by dispatch().
            tail: Max number of lines to return from the end (default: 200).
        """
        path = self._log_path(task_id)
        if not path.exists():
            return {"lines": [], "truncated": False, "total": 0, "error": "no log for task"}
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError as exc:
            return {"lines": [], "truncated": False, "total": 0, "error": str(exc)}
        return {
            "lines": lines[-tail:],
            "truncated": len(lines) > tail,
            "total": len(lines),
        }

    @emit()
    async def progress(self, task_id: str, step: str, detail: str = "") -> dict:
        """Streaming progress update."""

    @emit()
    async def work_done(self, task_id: str, branch: str, sha: str, summary: str) -> dict:
        """Task finished; feature branch was pushed."""

    @emit()
    async def work_failed(
        self,
        task_id: str,
        error: str,
        category: str = "unknown",
        step: str = "",
        log_tail: list[str] | None = None,
    ) -> dict:
        """Task failed; no branch was pushed.

        Categories: precondition | agent_error | no_changes | conflict |
        auth | rate_limit | cancelled | unknown.
        """

    def _log_path(self, task_id: str) -> Path:
        return self._log_dir / f"{task_id}.log"

    async def _run(
        self, task_id: str, prompt: str, base_ref: str, repo_path: Path, repo_name: str,
    ) -> None:
        branch = f"feature/{task_id}"
        current_step = "init"
        log_path = self._log_path(task_id)
        log_fh = log_path.open("w")
        try:
            current_step = "checkout"
            await self.progress(task_id=task_id, step=current_step, detail=f"{repo_name}@{base_ref}")
            if self._no_push:
                # No origin remote configured — branch straight from local ref.
                await self._git(repo_path, log_fh, "checkout", "-B", branch, base_ref)
            else:
                await self._git(repo_path, log_fh, "fetch", "origin", base_ref)
                await self._git(repo_path, log_fh, "checkout", "-B", branch, f"origin/{base_ref}")

            current_step = "agent"
            await self.progress(task_id=task_id, step=current_step)
            cmd = self._exec_cmd.format(prompt=shlex.quote(prompt))
            rc, stdout = await self._shell(cmd, cwd=repo_path, log_fh=log_fh)
            if rc != 0:
                raise _Fail("agent_error", f"agent exited {rc}", stdout[-400:].strip())

            rc, porcelain = await self._shell(
                "git status --porcelain", cwd=repo_path, log_fh=log_fh,
            )
            if rc != 0 or not porcelain.strip():
                raise _Fail("no_changes", "agent produced no changes", "")

            current_step = "commit"
            await self.progress(task_id=task_id, step=current_step)
            await self._git(repo_path, log_fh, "add", "-A")
            await self._git(repo_path, log_fh, "commit", "-m", f"{task_id}: {prompt[:60]}")

            if self._no_push:
                await self.progress(
                    task_id=task_id, step="push",
                    detail="skipped (--no-push; worker commits locally, dispatcher fetches)",
                )
            else:
                current_step = "push"
                await self.progress(task_id=task_id, step=current_step, detail=branch)
                await self._git(repo_path, log_fh, "push", "-u", "origin", branch)

            sha = (await self._capture(repo_path, log_fh, "git", "rev-parse", "HEAD")).strip()
            summary = (stdout.strip().splitlines() or ["done"])[-1][:400]
            self._results[task_id] = {
                "repo": repo_name, "branch": branch, "sha": sha, "summary": summary,
            }
            await self.work_done(task_id=task_id, branch=branch, sha=sha, summary=summary)

        except asyncio.CancelledError:
            self._results[task_id] = {
                "error": "cancelled", "category": "cancelled",
                "step": current_step, "repo": repo_name,
            }
            await self.work_failed(
                task_id=task_id, error="cancelled",
                category="cancelled", step=current_step, log_tail=[],
            )
            raise
        except _Fail as fail:
            tail = _read_log_tail(log_path, 20)
            self._results[task_id] = {
                "error": fail.message, "category": fail.category,
                "step": current_step, "repo": repo_name,
            }
            await self.work_failed(
                task_id=task_id, error=fail.message,
                category=fail.category, step=current_step, log_tail=tail,
            )
        except Exception as exc:
            category = _classify_exception(exc, current_step)
            tail = _read_log_tail(log_path, 20)
            self._results[task_id] = {
                "error": str(exc), "category": category,
                "step": current_step, "repo": repo_name,
            }
            await self.work_failed(
                task_id=task_id, error=str(exc),
                category=category, step=current_step, log_tail=tail,
            )
        finally:
            log_fh.close()

    async def _git(self, cwd: Path, log_fh, *args: str) -> None:
        rc, stdout = await self._shell(
            "git " + " ".join(shlex.quote(a) for a in args), cwd=cwd, log_fh=log_fh,
        )
        if rc != 0:
            category = _classify_git_failure(args[0], stdout)
            raise _Fail(category, f"git {args[0]} failed", stdout[-400:].strip())

    async def _capture(self, cwd: Path, log_fh, *args: str) -> str:
        rc, stdout = await self._shell(
            " ".join(shlex.quote(a) for a in args), cwd=cwd, log_fh=log_fh,
        )
        if rc != 0:
            raise _Fail("unknown", f"{args[0]} failed", stdout[-400:].strip())
        return stdout

    async def _shell(self, cmd: str, cwd: Path, log_fh=None) -> tuple[int, str]:
        """Run *cmd* in *cwd*, merging stdout+stderr. Streams to log_fh as lines arrive.

        Returns (returncode, combined_output).
        """
        if log_fh is not None:
            log_fh.write(f"\n$ {cmd}\n")
            log_fh.flush()
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        chunks: list[str] = []
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            text = raw.decode(errors="replace")
            chunks.append(text)
            if log_fh is not None:
                log_fh.write(text)
                log_fh.flush()
        rc = await proc.wait()
        return rc, "".join(chunks)


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
        "--log-dir",
        help=(
            "Directory for per-task stdout/stderr logs. Defaults to "
            "$XDG_STATE_HOME/coding-worker/logs or ~/.local/state/coding-worker/logs."
        ),
    )
    parser.add_argument(
        "--tenant",
        default=os.environ.get("TENANT", "default"),
        help="Tenant namespace (default: $TENANT or 'default').",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help=(
            "Commit locally but skip `git push`. Use when the working copy has "
            "no useful upstream (e.g. the dispatcher mirrored files here and will "
            "`git fetch` the worker's local refs directly instead)."
        ),
    )
    args = parser.parse_args()

    repos: dict[str, Path] = dict(args.repo)
    if not repos and args.repo_path:
        name, path = _parse_repo_flag(f"default={args.repo_path}")
        repos[name] = path
    if not repos:
        parser.error("must pass --repo NAME=PATH (repeatable) or --repo-path PATH")

    log_dir = Path(args.log_dir).expanduser().resolve() if args.log_dir else None

    driver = CodingWorkerDriver(
        exec_cmd=args.exec_cmd,
        repos=repos,
        default_repo=args.default_repo,
        log_dir=log_dir,
        no_push=args.no_push,
    )
    runtime = DeviceRuntime(driver=driver, device_id=args.device_id, tenant=args.tenant)
    asyncio.run(runtime.run())


if __name__ == "__main__":
    main()
