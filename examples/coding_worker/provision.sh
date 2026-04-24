#!/usr/bin/env bash
# Provision a remote Linux host as a Device Connect coding-worker over SSH.
#
# Usage:
#   ./provision.sh user@host [options]
#
# Defaults are picked to make the simplest case work:
#   ./provision.sh user@host
# brings up a worker named after the remote hostname, listening on tcp 7447,
# tenant=default, with a throwaway stub agent + bare git repo so you can
# dispatch immediately and see the round trip.
#
# Re-running is safe: missing pieces are added, existing pieces are reused.

set -euo pipefail

# ─────────── defaults ───────────
SSH_TARGET=""
DEVICE_ID=""
TENANT="default"
LISTEN_PORT="7447"
EXEC_CMD=""              # if empty, install + use stub-agent.sh
REPO_PATH=""             # existing working copy on the remote
SEED_FROM=""             # local path on the dispatcher — mirror it to the remote
SEED_FROM_URL=""         # git URL — clone directly on the remote
REPO_NAME=""             # override derived repo name for --seed-from-url
AT_PATH=""               # override remote destination path (rel to home or absolute)
WORKER_EXTRA_ARGS=""     # extra flags passed to coding_worker.py (e.g. --no-push)
REMOTE_PYTHON_VERSION="3.11"
PACKAGE_SPEC="device-connect-edge"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_WORKER="${SCRIPT_DIR}/coding_worker.py"
ACTION="install"
FORWARDED_AGENT_ENV_VARS=(
    OPENAI_API_KEY
    OPENAI_BASE_URL
    OPENAI_CA_CERT_PATH
    SSL_CERT_FILE
    REQUESTS_CA_BUNDLE
    CURL_CA_BUNDLE
    HTTPS_PROXY
    HTTP_PROXY
    ALL_PROXY
    NO_PROXY
    https_proxy
    http_proxy
    all_proxy
    no_proxy
)

# ─────────── arg parsing ───────────
usage() {
    cat <<EOF
Usage: $0 user@host [options]

Options:
  --device-id ID         Device id to register as (default: <hostname>-worker)
  --tenant T             Tenant namespace (default: default)
  --port N               TCP port for Zenoh listener (default: 7447)
  --exec-cmd 'CMD'       Coding agent command template; {prompt} substituted
                         (default: install + use a shell stub agent)

Repo (pick one; default creates a throwaway test repo):
  --repo PATH            Existing working copy on the remote
  --seed-from PATH       Local path on the dispatcher (must be under \$HOME).
                         Mirrors the path under \$HOME on the remote, pushes
                         HEAD to it, auto-adds a 'jetson' git remote on your
                         local repo. Worker commits locally (no push) — fetch
                         feature branches with 'git fetch jetson'.
  --seed-from-url URL    Git URL cloned on the remote. Default destination is
                         ~/<basename>. Worker pushes feature branches back to
                         this URL — remote host needs push credentials for it.
  --at PATH              Override remote destination (rel to remote \$HOME,
                         or absolute if starting with /).  Works with
                         --seed-from and --seed-from-url.
  --repo-name NAME       (--seed-from-url only) basename override.

  --package SPEC         pip spec for device-connect-edge (default: $PACKAGE_SPEC)
  --uninstall            Stop the service and remove the install
  -h, --help             This message
EOF
    exit 1
}

[[ $# -ge 1 ]] || usage
SSH_TARGET="$1"; shift
[[ "$SSH_TARGET" == *@* ]] || { echo "first arg must be user@host" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --device-id) DEVICE_ID="$2"; shift 2;;
        --tenant) TENANT="$2"; shift 2;;
        --port) LISTEN_PORT="$2"; shift 2;;
        --exec-cmd) EXEC_CMD="$2"; shift 2;;
        --repo) REPO_PATH="$2"; shift 2;;
        --seed-from) SEED_FROM="$2"; shift 2;;
        --seed-from-url) SEED_FROM_URL="$2"; shift 2;;
        --at) AT_PATH="$2"; shift 2;;
        --repo-name) REPO_NAME="$2"; shift 2;;
        --package) PACKAGE_SPEC="$2"; shift 2;;
        --uninstall) ACTION="uninstall"; shift;;
        -h|--help) usage;;
        *) echo "unknown option: $1" >&2; usage;;
    esac
done

# --repo / --seed-from / --seed-from-url are mutually exclusive
repo_opts=0
[[ -n "$REPO_PATH" ]]      && repo_opts=$((repo_opts + 1))
[[ -n "$SEED_FROM" ]]      && repo_opts=$((repo_opts + 1))
[[ -n "$SEED_FROM_URL" ]]  && repo_opts=$((repo_opts + 1))
if (( repo_opts > 1 )); then
    echo "choose one of --repo, --seed-from, --seed-from-url" >&2
    exit 2
fi

[[ -f "$LOCAL_WORKER" ]] || { echo "missing $LOCAL_WORKER" >&2; exit 2; }

REMOTE_HOST="${SSH_TARGET#*@}"

# ─────────── small helpers ───────────
say()  { printf "\033[1;36m▎\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m✓\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m!\033[0m %s\n" "$*" >&2; }

ssh_run() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$SSH_TARGET" "$@"; }

# Run a multi-line bash script on the remote. Feeds the script to `bash -s`
# via stdin — more reliable than passing it as a quoted argv (which, depending
# on the remote login shell, can silently truncate).
ssh_script() {
    ssh -T -o BatchMode=yes -o ConnectTimeout=10 "$SSH_TARGET" bash -s
}

# ─────────── uninstall path ───────────
if [[ "$ACTION" == "uninstall" ]]; then
    say "stopping coding-worker on $SSH_TARGET"
    ssh_script <<'REMOTE'
systemctl --user stop coding-worker 2>/dev/null || true
systemctl --user disable coding-worker 2>/dev/null || true
rm -f ~/.config/systemd/user/coding-worker.service
systemctl --user daemon-reload 2>/dev/null || true
pkill -f coding_worker.py 2>/dev/null || true
rm -rf ~/.coding-worker ~/coding_worker.py ~/stub-agent.sh ~/start-worker.sh ~/worker-logs
echo done
REMOTE
    ok "uninstalled."
    exit 0
fi

# ─────────── derive defaults ───────────
if [[ -z "$DEVICE_ID" ]]; then
    DEVICE_ID="$(ssh_run 'hostname')-worker"
    say "device-id (auto): $DEVICE_ID"
fi

# ─────────── 1. ensure Python 3.11 via uv ───────────
say "checking remote Python on $SSH_TARGET"
PY_VERSION="$(ssh_run "python3 -V 2>&1 | awk '{print \$2}'")"
say "remote python3: ${PY_VERSION}"

ssh_run 'set -e
mkdir -p ~/.coding-worker
if ! command -v uv >/dev/null 2>&1 && [[ ! -x ~/.local/bin/uv ]]; then
    echo "installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
fi
export PATH=$HOME/.local/bin:$PATH
if [[ ! -x ~/.coding-worker/venv/bin/python ]]; then
    uv python install '"$REMOTE_PYTHON_VERSION"' >/dev/null 2>&1 || true
    uv venv --python '"$REMOTE_PYTHON_VERSION"' ~/.coding-worker/venv >/dev/null
fi
uv pip install --quiet --python ~/.coding-worker/venv/bin/python '"$PACKAGE_SPEC"' >/dev/null
~/.coding-worker/venv/bin/python -c "import device_connect_edge" \
    && echo "device-connect-edge: ok"'
ok "python venv + device-connect-edge ready"

# ─────────── 2. push the worker script ───────────
say "uploading coding_worker.py"
scp -q "$LOCAL_WORKER" "${SSH_TARGET}:~/.coding-worker/coding_worker.py"
ok "worker script uploaded"

# ─────────── 3. stub agent + test repo (only if not provided) ───────────
if [[ -z "$EXEC_CMD" ]]; then
    say "no --exec-cmd given — installing stub agent for testing"
    ssh_run 'cat > ~/.coding-worker/stub-agent.sh <<'"'"'EOF'"'"'
#!/bin/bash
# Tiny stand-in for a real coding agent — appends a timestamped line to a
# tracked file. Lets you exercise the full dispatch round-trip without
# installing codex/claude/aider.
set -e
mkdir -p notes
ts="$(date -Iseconds)"
echo "${ts}: $*" >> notes/auto-change.md
echo "stub-agent wrote notes/auto-change.md"
EOF
chmod +x ~/.coding-worker/stub-agent.sh
echo "stub-agent: ok"'
    EXEC_CMD='$HOME/.coding-worker/stub-agent.sh {prompt}'
fi

# Always-idempotent git identity on the remote (only sets if missing).
ssh_script <<'REMOTE'
git config --global user.email >/dev/null 2>&1 || git config --global user.email "coding-worker@$(hostname)"
git config --global user.name  >/dev/null 2>&1 || git config --global user.name  "coding-worker"
git config --global init.defaultBranch >/dev/null 2>&1 || git config --global init.defaultBranch main
REMOTE

# Helper: derive repo name from a path or URL if not explicitly set.
derive_repo_name() {
    local src="$1"
    local base="${src##*/}"
    base="${base%.git}"
    echo "$base"
}

# Normalize a remote-destination string. Callers pass either:
#   "workplace/foo"     — treated as relative to remote $HOME
#   "~/workplace/foo"   — same, with redundant ~/ stripped
#   "/absolute/path"    — used as-is
# Sets three globals so the rest of the script can reference them:
#   JETSON_DEST_NORM   — normalized (rel without ~/ prefix, or absolute)
#   REMOTE_PATH_EXPR   — reference usable from remote bash: $HOME/foo or /abs
#   REMOTE_PARENT_EXPR — parent dir of REMOTE_PATH_EXPR
normalize_remote_dest() {
    local raw="$1"
    raw="${raw#\~/}"
    JETSON_DEST_NORM="$raw"
    if [[ "$JETSON_DEST_NORM" == /* ]]; then
        REMOTE_PATH_EXPR="$JETSON_DEST_NORM"
    else
        REMOTE_PATH_EXPR="\$HOME/$JETSON_DEST_NORM"
    fi
    REMOTE_PARENT_EXPR="$(dirname "$REMOTE_PATH_EXPR")"
}

# ─────────── 3a. --seed-from PATH (mirror-path, no bare) ───────────
JETSON_BARE_REMOTE_NOTE=""
if [[ -n "$SEED_FROM" ]]; then
    [[ -d "$SEED_FROM" ]] || { echo "--seed-from path does not exist: $SEED_FROM" >&2; exit 2; }
    [[ -d "$SEED_FROM/.git" || -f "$SEED_FROM/.git" ]] || { echo "$SEED_FROM is not a git working copy" >&2; exit 2; }
    SEED_FROM_ABS="$(cd "$SEED_FROM" && pwd)"

    # Derive the remote destination:
    #   - if --at is set, use that
    #   - else require SEED_FROM_ABS to live under local $HOME and mirror the
    #     relative sub-path on the remote under its $HOME.
    if [[ -n "$AT_PATH" ]]; then
        normalize_remote_dest "$AT_PATH"
    else
        case "$SEED_FROM_ABS" in
            "$HOME"|"$HOME"/*) : ;;
            *)
                echo "--seed-from PATH must live under \$HOME (${HOME}) so we can mirror" >&2
                echo "  it under \$HOME on the remote; or pass --at PATH explicitly." >&2
                exit 2
                ;;
        esac
        normalize_remote_dest "${SEED_FROM_ABS#$HOME/}"
    fi

    say "mirroring $SEED_FROM_ABS → $SSH_TARGET:${REMOTE_PATH_EXPR}"

    # Create a non-bare git repo at the target on the remote. Configure it to
    # accept pushes to its currently-checked-out branch via updateInstead.
    ssh_script <<REMOTE
set -e
mkdir -p ${REMOTE_PARENT_EXPR}
mkdir -p ${REMOTE_PATH_EXPR}
cd ${REMOTE_PATH_EXPR}
if [[ ! -d .git ]]; then
    git init --quiet
fi
git config receive.denyCurrentBranch updateInstead
git config receive.denyDeleteCurrent ignore
REMOTE

    # Push from local; scp-style URL with relative (home-anchored) path works
    # for home-relative destinations. For absolute paths, format accordingly.
    if [[ "$JETSON_DEST_NORM" == /* ]]; then
        JETSON_REMOTE_URL="ssh://${SSH_TARGET}${JETSON_DEST_NORM}"
    else
        JETSON_REMOTE_URL="${SSH_TARGET}:${JETSON_DEST_NORM}"
    fi
    # Mirror every local branch so the worker can use any of them as base_ref.
    # Two pushes:
    #   1) HEAD:main forces remote main to track the dispatcher's current HEAD,
    #      so the worker's working tree (checked out to main on the remote) is
    #      populated even when local HEAD is on a feature branch.
    #   2) --all copies any other local branches as plain refs (not checked
    #      out — the worker uses them as starting points via `git checkout -B`).
    (
        cd "$SEED_FROM_ABS"
        git push --quiet "${JETSON_REMOTE_URL}" HEAD:main
        # --all may complain if there are no other branches; ignore that case.
        git push --quiet "${JETSON_REMOTE_URL}" --all 2>/dev/null || true
        if git remote get-url jetson >/dev/null 2>&1; then
            git remote set-url jetson "${JETSON_REMOTE_URL}"
        else
            git remote add jetson "${JETSON_REMOTE_URL}"
        fi
    )

    REPO_PATH="${REMOTE_PATH_EXPR}"
    WORKER_EXTRA_ARGS="--no-push"
    JETSON_BARE_REMOTE_NOTE=$'\n'"Local 'jetson' remote configured on ${SEED_FROM_ABS}."$'\n'"Worker commits locally; dispatcher fetches refs directly. Fetch with:"$'\n'"  cd ${SEED_FROM_ABS} && git fetch jetson"
    ok "mirrored to ${REMOTE_PATH_EXPR} (local 'jetson' remote → ${JETSON_REMOTE_URL})"
fi

# ─────────── 3b. --seed-from-url URL (clone directly, no bare) ───────────
if [[ -n "$SEED_FROM_URL" ]]; then
    if [[ -n "$AT_PATH" ]]; then
        normalize_remote_dest "$AT_PATH"
    else
        BASENAME="${REPO_NAME:-$(derive_repo_name "$SEED_FROM_URL")}"
        normalize_remote_dest "$BASENAME"
    fi

    say "cloning $SEED_FROM_URL → $SSH_TARGET:${REMOTE_PATH_EXPR}"
    ssh_script <<REMOTE
set -e
mkdir -p ${REMOTE_PARENT_EXPR}
if [[ ! -d ${REMOTE_PATH_EXPR} ]]; then
    git clone --quiet "${SEED_FROM_URL}" ${REMOTE_PATH_EXPR}
fi
REMOTE
    REPO_PATH="${REMOTE_PATH_EXPR}"
    # Worker pushes feature branches back to SEED_FROM_URL (origin); no --no-push.
    JETSON_BARE_REMOTE_NOTE=$'\n'"The worker pushes feature branches back to ${SEED_FROM_URL}."$'\n'"Make sure ${SSH_TARGET} has push credentials for that URL."
    ok "cloned to ${REMOTE_PATH_EXPR}"
fi

# ─────────── 3c. fallback throwaway test repo ───────────
if [[ -z "$REPO_PATH" ]]; then
    say "no --repo / --seed-from* given — creating throwaway test repo at ~/work/test-repo"
    ssh_run 'set -e
if [[ ! -d /tmp/test-repo.git ]]; then
    git init --bare /tmp/test-repo.git >/dev/null
fi
if [[ ! -d ~/work/test-repo ]]; then
    mkdir -p ~/work
    git clone /tmp/test-repo.git ~/work/test-repo >/dev/null 2>&1
    cd ~/work/test-repo
    if ! git rev-parse HEAD >/dev/null 2>&1; then
        echo "# test-repo" > README.md
        git add README.md
        git commit -m "initial" >/dev/null
        git push -u origin main >/dev/null 2>&1
    fi
fi
echo "test repo: ok"'
    REPO_PATH='~/work/test-repo'
fi

# ─────────── 4. install wrapper script + systemd user service ───────────
say "writing run wrapper and systemd user service"

say "capturing codex/proxy env from remote login shell"
FORWARDED_AGENT_ENV_VARS_STR="${FORWARDED_AGENT_ENV_VARS[*]}"
ssh_script <<REMOTE
set -euo pipefail
mkdir -p ~/.coding-worker
if command -v getent >/dev/null 2>&1; then
    LOGIN_SHELL="\$(getent passwd "\$USER" | cut -d: -f7)"
fi
LOGIN_SHELL="\${LOGIN_SHELL:-\${SHELL:-/bin/bash}}"
if [[ ! -x "\$LOGIN_SHELL" ]]; then
    LOGIN_SHELL=/bin/bash
fi
FORWARDED_AGENT_ENV_VARS_STR='${FORWARDED_AGENT_ENV_VARS_STR}'
"\$LOGIN_SHELL" -ilc 'env -0' | FORWARDED_AGENT_ENV_VARS_STR="\$FORWARDED_AGENT_ENV_VARS_STR" python3 -c '
import os
import pathlib
import shlex
import sys

allowed = set(os.environ["FORWARDED_AGENT_ENV_VARS_STR"].split())
entries = sys.stdin.buffer.read().split(b"\0")
lines = []
for entry in entries:
    if not entry:
        continue
    key, sep, value = entry.partition(b"=")
    if not sep:
        continue
    name = key.decode("utf-8", "replace")
    text = value.decode("utf-8", "replace")
    if name in allowed and text:
        lines.append(f"export {name}={shlex.quote(text)}\n")
target = pathlib.Path.home() / ".coding-worker" / "agent-env.sh"
target.write_text("".join(lines), encoding="utf-8")
target.chmod(0o600)
print(f"captured {len(lines)} env vars into {target}")
'
REMOTE

# Wrapper script — keeps systemd's argv parser out of the way.
ssh_run "cat > ~/.coding-worker/run.sh" <<RUNSH
#!/usr/bin/env bash
set -euo pipefail
export DEVICE_CONNECT_ALLOW_INSECURE=true
export MESSAGING_BACKEND=zenoh
export ZENOH_LISTEN=tcp/0.0.0.0:${LISTEN_PORT}
if [[ -f "\$HOME/.coding-worker/agent-env.sh" ]]; then
    # shellcheck disable=SC1090
    source "\$HOME/.coding-worker/agent-env.sh"
fi
exec "\$HOME/.coding-worker/venv/bin/python" \\
    "\$HOME/.coding-worker/coding_worker.py" \\
    --device-id "${DEVICE_ID}" \\
    --tenant "${TENANT}" \\
    --exec-cmd "${EXEC_CMD}" \\
    --repo-path "${REPO_PATH}" ${WORKER_EXTRA_ARGS}
RUNSH
ssh_run "chmod +x ~/.coding-worker/run.sh"

ssh_run "mkdir -p ~/.config/systemd/user"
ssh_run "cat > ~/.config/systemd/user/coding-worker.service" <<UNIT
[Unit]
Description=Device Connect coding worker
After=network-online.target

[Service]
Type=simple
ExecStart=%h/.coding-worker/run.sh
Restart=on-failure
RestartSec=2
StandardOutput=append:%h/.coding-worker/worker.log
StandardError=append:%h/.coding-worker/worker.log

[Install]
WantedBy=default.target
UNIT

ssh_run 'systemctl --user daemon-reload
loginctl enable-linger "$USER" >/dev/null 2>&1 || true
systemctl --user enable coding-worker >/dev/null
systemctl --user restart coding-worker
sleep 2
systemctl --user is-active coding-worker'

ok "service started"

# ─────────── 5. verify port ───────────
say "verifying Zenoh listener on tcp/${LISTEN_PORT}"
if ssh_run "ss -tln 2>/dev/null | grep -q ':${LISTEN_PORT} '"; then
    ok "listening on tcp/0.0.0.0:${LISTEN_PORT}"
else
    warn "port ${LISTEN_PORT} not listening yet — check 'ssh ${SSH_TARGET} \"systemctl --user status coding-worker; tail ~/.coding-worker/worker.log\"'"
fi

# ─────────── 6. emit the codex MCP config the dispatcher should use ───────────
cat <<EOF

────────────────────────────────────────────────────────────────────
Worker is live. To dispatch from your codex (or any MCP client),
add the following to your codex config (e.g. ~/.codex/config.toml):

[mcp_servers.device-connect]
command = "${SCRIPT_DIR%/examples/coding_worker}/.venv/bin/python"
args = ["-m", "device_connect_agent_tools.mcp"]

[mcp_servers.device-connect.env]
DEVICE_CONNECT_ALLOW_INSECURE = "true"
MESSAGING_BACKEND = "zenoh"
ZENOH_CONNECT = "tcp/${REMOTE_HOST}:${LISTEN_PORT}"
DEVICE_CONNECT_DISCOVERY_MODE = "d2d"
TENANT = "${TENANT}"

Worker details
  device_id   : ${DEVICE_ID}
  tenant      : ${TENANT}
  repo path   : ${REPO_PATH}
  exec cmd    : ${EXEC_CMD}
  service log : ssh ${SSH_TARGET} 'tail -f ~/.coding-worker/worker.log'
${JETSON_BARE_REMOTE_NOTE}
To stop / remove:
  $0 ${SSH_TARGET} --uninstall
────────────────────────────────────────────────────────────────────
EOF
