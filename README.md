# SandboxHub

A self-hosted sandbox orchestration service that manages isolated Docker containers for LLM/VLM agents. Built on the architecture patterns of [Anthropic's Claude Computer Use demo](https://github.com/anthropics/claude-quickstarts/tree/main/computer-use-demo), extended with a warm-pool orchestration layer, multi-sandbox management, and real-time terminal streaming.

> 中文文档：[README_CN.md](./README_CN.md)

---

## Overview

SandboxHub has two components that live in this monorepo:

| Component | Path | Role |
|-----------|------|------|
| **Orchestrator** | `src/` | Manages container lifecycle — warm pool, acquire/release, HTTP proxy |
| **Ubuntu Image** | `images/ubuntu/` | Ubuntu 22.04 sandbox — virtual desktop, FastAPI tool API, MCP server |
| **Code Image** | `images/code/` | Headless lightweight sandbox — Python + Node toolchain, dev/office libs, playwright headless chromium, terminal/file/system/process API only (no GUI), sub-second cold start |

```
LLM Agent
    │  POST /v1/sandboxes/acquire
    ▼
SandboxHub :8088  ─── warm pool ──→  Ubuntu Container
    │                                   ├─ FastAPI  :8000  (40+ REST tools)
    │  proxy /v1/sandboxes/{id}/proxy/  ├─ FastMCP  :8001  (30+ MCP tools)
    ▼                                   ├─ noVNC    :6080  (web desktop)
  response                              └─ VNC      :5900
```

### Relation to Claude Computer Use

The Ubuntu sandbox image is directly inspired by Anthropic's [computer-use-demo](https://github.com/anthropics/claude-quickstarts/tree/main/computer-use-demo). Core design patterns carried over:

- **Terminal as tmux jobs** — every command runs as a tmux window under `script(1)` (a real PTY), so interactive programs work and humans can `tmux attach` (`images/ubuntu/app/tools/bash.py`)
- **`ToolResult` / `CLIResult` abstractions** — structured tool output for LLM consumption
- **Virtual desktop stack** — TigerVNC + openbox + noVNC for VLM screenshot-and-click workflows
- **Tool injection via lifespan** — `BashTool`, `ComputerTool`, `EditTool` singletons injected into FastAPI routers at startup

SandboxHub adds on top:
- **Warm pool** — pre-warmed containers eliminate cold-start latency (<100ms acquire)
- **Registry** — tracks allocated containers per `(user_id, role_id)` pair, enables reuse
- **HTTP proxy layer** — single ingress point; routes all tool calls to the right container
- **Reconciler** — self-healing lifecycle: health-check on acquire (dead sandboxes evicted and transparently re-created), startup recovery after host/service restarts (stopped containers removed, leftover warm containers reset then re-adopted), periodic sweep that destroys untracked orphan containers and auto-reclaims idle sandboxes
- **Job-based terminal** — `execute(wait)` / `wait(cursor)` / `kill`: commands run as jobs (tmux windows) in a per-conversation tmux session (cwd / exported env survive across calls), several jobs may run in parallel, callers long-poll in chunks, no default timeout and no upper bound (issue #30); full output lands in `/tmp/cr-jobs/<job_id>.log`
- **SSE streaming** — `POST /api/terminal/execute/stream` streams stdout in real-time (extends the original polling model)
- **Multi-arch Dockerfile** — builds on both amd64 (Google Chrome) and arm64 (Chromium)

---

## Quick Start

### 1. Build the sandbox image

Preferred: the build script (tags both `latest` and the version from `images/ubuntu/app/VERSION`):

```bash
scripts/build-images.sh          # build code + ubuntu
scripts/build-images.sh code     # code only
NO_LATEST=1 scripts/build-images.sh fs   # cr-fs uses images/fs/VERSION
```

Manual builds:

```bash
# Full desktop image (GUI + browser + skills)
docker build -t sandbox-ubuntu:latest images/ubuntu/

# Lightweight headless code image (Python + Node + dev/office libs, no GUI)
# Build context is images/ (shares ubuntu/app code), so -f + context are required:
docker build -f images/code/Dockerfile -t sandbox-code:latest images
```

> **Image version reconciliation (deployment-drift guard):** when changing app code under `images/`, bump
> `images/ubuntu/app/VERSION` and rebuild. Containers report their `app_version` via `GET /api/system/health`;
> the reconciler periodically compares it against the repo's `images/ubuntu/app/VERSION` and logs a
> `镜像版本漂移` warning on mismatch — "code merged but image never rebuilt" no longer drifts silently (issue #6).

> **Network note (China):** Both Dockerfiles use domestic mirrors — TUNA for APT/pip and npmmirror for Node/npm — so no proxy is needed for APT/pip/npm.
> The **code** image is fully buildable behind the Great Firewall with no proxy. The **ubuntu** image additionally pulls these from overseas (proxy recommended):
> - Google Chrome / Chromium (arm64)
> - noVNC, websockify (GitHub)
> - pyenv (GitHub)

> **Code image toolchain:** Python 3.11 + Node 20 (yarn/pnpm), `git`/`ripgrep`/`fd`/`jq`/`vim`, build-essential, daily Python libs (pandas, openpyxl, python-docx/pptx, reportlab, pypdf, pymupdf, matplotlib, markitdown with docx/xlsx/pdf extras…), legacy Office readers (olefile, xlrd, msoffcrypto-tool, `antiword`, `catdoc`), `file`/`xxd` and `poppler-utils`. Agents can introspect it at runtime via `GET /api/system/env` or read `/etc/sandbox/MANIFEST.md` (includes a file-format → tool table).

> **Engine runtime (code image):** the platform-hosted execution engines live in `/opt/cr-engines`, separate from the system Node 20: a Node 24 LTS (`/opt/cr-engines/node`) plus pinned `@earendil-works/pi-coding-agent` (bin `pi`) and `@deepseek-ai/dsh` (bin `dsh`). `/opt/cr-engines/bin/pi` and `/opt/cr-engines/bin/dsh` are wrappers that always run that Node, whatever `PATH` says; they are not on `PATH` (the platform starts them through `/api/procs`). Build args: `ENGINE_NODE_VERSION` (default 24.22.1), `PI_VERSION` (1.4.0), `DSH_VERSION` (0.2.1-rc.3 — pinned exactly, npm `latest` is still rc.2), `NPM_REGISTRY` (npmmirror) and `ENGINE_ALLOW_SCRIPTS` (npm 11 skips dependency install scripts unless allowed; node-pty / koffi need theirs). The build fails unless `pi --version` / `dsh --version` print the requested versions, node-pty works, and one DSH ACP `initialize` and one pi RPC `get_state` round-trip succeed. The image is labelled `sandboxhub.procs="1"` and `sandboxhub.engines='{"pi":"…","dsh":"…"}'` (the ubuntu image carries `sandboxhub.procs` only); the engine runtime adds about 1.3 GB.

#### Building with a proxy

If you have a local proxy (e.g. v2ray/clash) listening on `127.0.0.1:8118`, use `--network host` so build-time `RUN` commands can reach it:

```bash
# Adjust to match your proxy settings
PROXY_HOST="127.0.0.1"
HTTP_PORT="8118"
HTTP_PROXY_URL="http://${PROXY_HOST}:${HTTP_PORT}"

docker build --network host \
  --build-arg HTTP_PROXY=${HTTP_PROXY_URL} \
  --build-arg HTTPS_PROXY=${HTTP_PROXY_URL} \
  --build-arg http_proxy=${HTTP_PROXY_URL} \
  --build-arg https_proxy=${HTTP_PROXY_URL} \
  -t sandbox-ubuntu:latest images/ubuntu/
```

> `--network host` shares the host network stack with build-stage `RUN` commands, allowing them to reach a proxy bound to `127.0.0.1`.

### 2. Install and configure SandboxHub

```bash
pip install -e .

cp .env.example .env   # connection / secret / host-local items only
```

Configuration lives in two homes (issue #28, mirroring createrole#400):

- `.env` (not in git): host, network, MinIO connection, API key — a fresh deployment only fills this file.
- `config/system.yaml` (in git, changed via PR): image names, warm pool, reconcile/reclaim, proxy timeouts, rclone mount policy.
  Precedence: file > code default; a missing/invalid key falls back per key with a warning; **no env override** — the retired
  same-named env vars are warned about and ignored at startup.

```env
SANDBOX_HUB_PORT=8088
MINIO_ENDPOINT=172.17.0.1:9000
MINIO_ACCESS_KEY=...
MINIO_SECRET_KEY=...
```

### 3. Start SandboxHub

```bash
python main.py
```

Or with uvicorn directly:

```bash
uvicorn src.main:app --host 0.0.0.0 --port 8088 --reload
```

### 4. Health check

```bash
curl http://localhost:8088/v1/health
# {"ok": true, "warm_pool": {"ubuntu": {"available": 3, "allocated": 0}}}
```

---

## API

### Acquire a sandbox

```bash
curl -X POST http://localhost:8088/v1/sandboxes/acquire \
  -H "Content-Type: application/json" \
  -d '{"user_id": "u1", "role_id": "r1", "sandbox_type": "ubuntu"}'
# → {"sandbox_id": "sb_abc123", "status": "ready"}
```

Returns in <100ms from the warm pool. Reuses an existing container if the same `(user_id, role_id)` pair already has one allocated.

#### Acquire with a mounted workspace (MinIO ↔ container)

```bash
curl -X POST http://localhost:8088/v1/sandboxes/acquire \
  -H "Content-Type: application/json" \
  -d '{"user_id": "u1", "role_id": "r1", "sandbox_type": "code",
       "workspace": {"bucket": "createrole-workspaces", "prefix": "roles/r1", "mount_path": "/workspace"}}'
```

When `workspace` is present and SandboxHub has MinIO credentials configured (see Configuration),
the container is cold-started with FUSE capabilities and `rclone mount`s the MinIO `bucket/prefix`
at `mount_path` (default `/workspace`). Files written under the mount propagate to MinIO
near-real-time (rclone VFS write-back), and objects added to the prefix in MinIO become visible
in the container — i.e. the role's cloud drive and its sandbox share one filesystem.

Mounted sandboxes are **role-dedicated**: they bypass the warm pool and are destroyed (after
unmount) on release — never recycled and never `rm -rf`'d (which would delete MinIO data).
If credentials are missing or `WORKSPACE_MOUNT_ENABLED=false`, the `workspace` field is ignored
(logged as a warning) and an ordinary unmounted container is returned. Requires `rclone` + `fuse3`
in the image (both bundled) and the host permitting `/dev/fuse` + `SYS_ADMIN` for the container.

#### Acquire with injected environment variables (issue #15/#16)

```bash
curl -X POST http://localhost:8088/v1/sandboxes/acquire \
  -H "Content-Type: application/json" \
  -d '{"user_id": "u1", "role_id": "r1", "sandbox_type": "code",
       "env": {"CR_API_BASE": "http://host.docker.internal:8011", "CR_SANDBOX_TOKEN": "..."}}'
```

Key/value pairs in `env` are injected as container environment variables at creation time
(e.g. for in-sandbox CLIs like `skills` / `todo` calling back to the backend). Semantics:

- **Creation-time only** — ignored when reusing an existing sandbox for the same
  `(user_id, role_id)` (callers use sliding-renewal tokens, so the initially injected value stays valid);
- **Bypasses the warm pool** — pooled containers were created without these vars and Docker
  cannot inject env into a running container, so a first allocation with `env` cold-starts;
- **Tenant-dedicated** — values may be credentials: the container is destroyed on release
  (never returned to the shared pool), and logs record env keys only, never values;
- Omitting `env` keeps the exact current behavior.

### Execute a terminal command (job contract)

Commands run as **jobs** — each one is a tmux window (named by `job_id`) inside a tmux session
named after the caller's conversation (`session` field, default `default`). Within a session
`cd` / `export` / `source venv` carry over to the next call; different sessions cannot see each
other. Several jobs may run at once. The command runs under `script(1)` in a real PTY, so
interactive programs work: from another job in the same session the model can
`tmux send-keys -t <job_id> 'text' Enter`, `tmux capture-pane -p -t <job_id>`, or
`tmux kill-window -t <job_id>`. `wait` (default 30, server cap 120) bounds how long *this
request* blocks; `timeout` is the command's total time limit — **no default, no upper bound**,
omit it and the command runs until it finishes. Full output is written to `log_path` inside the
container (`tail` / `grep` it from the sandbox); the `output` field is head/tail-truncated at
25 KB + 25 KB, with CRLF / escape sequences normalised.

```bash
# submit; returns after ≤60s with the job state so far
curl -X POST http://localhost:8088/v1/sandboxes/sb_abc123/proxy/api/terminal/execute \
  -H "Content-Type: application/json" \
  -d '{"command": "pip install openai-whisper", "wait": 60, "session": "conv-42"}'
# → {"job_id": "j_01K…", "tmux_session": "conv-42", "status": "running", "exit_code": null,
#    "output": "…so far…", "cursor": 4096, "log_path": "/tmp/cr-jobs/j_01K….log",
#    "kill_reason": null, "success": true}

# long-poll: incremental output from `cursor`; returns immediately once the job has ended
curl -X POST http://localhost:8088/v1/sandboxes/sb_abc123/proxy/api/terminal/wait \
  -H "Content-Type: application/json" \
  -d '{"job_id": "j_01K…", "cursor": 4096, "wait": 60}'
# → {"job_id": "j_01K…", "status": "exited", "exit_code": 0, "output": "…", "cursor": 9120, …}

# stop it (the caller does this when the user cancels the turn)
curl -X POST http://localhost:8088/v1/sandboxes/sb_abc123/proxy/api/terminal/kill \
  -H "Content-Type: application/json" \
  -d '{"job_id": "j_01K…", "signal": "INT"}'     # INT | TERM | KILL
# → {"status": "killed", "exit_code": 130, "kill_reason": "kill:INT", …}
```

`status` is `running | exited | killed`; `kill_reason` is `timeout`, `kill:<SIG>`, `restart`,
`evicted` (legacy records only) or `window_closed` (someone ran
`tmux kill-window`). `POST /api/terminal/restart` (`{"session": "…"}`) kills that session's
running jobs, destroys its tmux session and resets cwd / env.

The server retains a capacity of 64 live windows and rejects new commands when full;
existing jobs keep running. `GET /api/terminal/activity` reports `running_jobs`,
`live_panes`, and `marked_processes` (running long-lived procs are included in
`running_jobs` and also reported as `running_procs`, see below). Each job passes an internal, non-secret ID to
its child processes, so `/proc` activity also protects `nohup`, background `&`, and
`setsid` descendants after their shell exits or the job record is pruned. The marker
is excluded from saved session environment; zombies do not count. Idle reclaim
requires all three counters to be zero. Unreadable process activity, missing fields,
and unavailable or old container APIs are preserved rather than assumed idle.
The same response carries the job-origin ledger used by v2 `GET /v2/work-areas/{id}/jobs`:
`jobs` (`job_id`, `session_id`, `turn_id`, `started_at`, `ended_at` as epoch seconds) and
`unattributed_live` (live marked jobs without an origin record). A job ends once its
window has exited and no marked descendant remains (tracked about once per second).

`POST /api/file/view` accepts `max_chars` (default 16000) and `column` (default 0),
and returns `path`, `next_offset`, `next_column`, `truncated`, and `total_lines`
(known only at EOF). Continue using both returned offsets, including when a long line
spans multiple pages. Reads use bounded fragments instead of loading the whole file.
The response text includes the continuation parameters for older callers.

`POST /api/file/upload` has no business size cap by default. The Hub streams request
bodies; the container copies multipart temporary storage in 1MiB chunks and replaces
the destination atomically. Failures preserve an existing destination and remove the
temporary copy. Storage exhaustion during multipart parsing or destination writes
returns 507 with the affected path. Incomplete multipart files are also closed on
errors or cancelled uploads; the multipart fields and OpenAPI schema are unchanged.

**Legacy form** (kept for a transition period): a body **without `wait`** blocks until the command
ends, with `timeout` defaulting to 30s (max 300s), and the response still carries
`success` / `output` / `error` / `system` (plus the job fields):

```bash
curl -X POST http://localhost:8088/v1/sandboxes/sb_abc123/proxy/api/terminal/execute \
  -H "Content-Type: application/json" \
  -d '{"command": "ls /workspace", "timeout": 30}'
# → {"success": true, "output": "...", "error": "", "status": "exited", "exit_code": 0, …}
```

### Stream terminal output (SSE)

```bash
curl -X POST http://localhost:8088/v1/sandboxes/sb_abc123/proxy/api/terminal/execute/stream \
  -H "Content-Type: application/json" \
  -d '{"command": "python train.py"}' \
  --no-buffer
# data: {"type": "stdout", "chunk": "Epoch 1/10\n"}
# data: {"type": "stdout", "chunk": "loss: 0.42\n"}
# data: {"type": "done"}
```

### Long-lived processes (`/api/procs`)

Programs driven over stdio — the platform's execution engines (pi `--mode rpc`, DeepSeek Harness
`--profile acp`) — run as **procs** (SandboxHub#52). The container app holds their pipes directly
(no tmux / PTY, no output cleaning): stdin is writable with backpressure and closable, stdout and
stderr are recorded **separately and byte-exact** to append-only logs addressed by absolute byte
offsets. A proc belongs to the app, not to the request that started it: client disconnects,
cancelled long-polls and backend restarts do not touch it — reattach by `proc_id` (or find it by
`name`) and resume reading from your cursors. There is **no lifetime cap** (`job_max_lifetime` does
not apply); the caller removes it. Through v2, every call below goes to
`/v2/sandboxes/{id}/proxy/...` with `X-API-Key`.

```bash
P=http://localhost:8088/v2/sandboxes/sb_abc123/proxy
# start: reset_dirs are wiped, then files written, then the process spawned
curl -X POST $P/api/procs -H "X-API-Key: $KEY" -H "Content-Type: application/json" -d '{
  "name": "engine:conv-42", "argv": ["/opt/cr-engines/bin/dsh", "--profile", "acp"],
  "env": {"DSH_HOME": "/tmp/cr-engines/conv-42"}, "inherit_env": ["PATH", "HOME", "LANG", "TZ"],
  "cwd": "/workspace/work", "reset_dirs": ["/tmp/cr-engines/conv-42"],
  "files": [{"path": "/tmp/cr-engines/conv-42/profiles/acp/user.yml", "content_b64": "…", "mode": 384}],
  "log_retain_bytes": 8388608}'
# → 201 {"proc_id": "p_01K…", "name": "engine:conv-42", "pid": 312, "state": "running",
#        "exit_code": null, "started_at": "2026-10-25T08:00:00.000000Z", "ended_at": null,
#        "stdin_open": true, "stdout_size": 0, "stderr_size": 0}

# write stdin: returns once every byte is in the pipe (blocks while the child is not reading)
curl -X POST $P/api/procs/p_01K…/stdin -d '{"data_b64": "eyJqc29ucnBjIjoi…", "close": false}'
# → {"written": 512, "stdin_open": true}

# long-poll from byte cursors: returns as soon as either stream has bytes past its cursor or the
# proc is no longer running, otherwise after `wait` seconds (server cap 120)
curl -X POST $P/api/procs/p_01K…/read -d '{"stdout_cursor": 0, "stderr_cursor": 0, "wait": 60}'
# → {"stdout_b64": "…", "stdout_cursor": 4096, "stderr_b64": "", "stderr_cursor": 0,
#    "truncated": false, "proc": {…ProcessInfo…}}

curl -X POST $P/api/procs/p_01K…/signal -d '{"signal": "TERM"}'   # INT | TERM | HUP | KILL → {"ok": true}
curl -X DELETE $P/api/procs/p_01K…        # KILL the group if running, drop record + logs → {"ok": true}
curl "$P/api/procs?name=engine:conv-42"   # → {"procs": [ProcessInfo, …]} (all procs without name)
```

Errors are `{"detail": {"reason": …, "message": …}}`: `400 invalid_request` (also for malformed
bodies, a missing executable or cwd), `404 proc_not_found`, `409 proc_exists` (with `proc_id`),
`409 stdin_closed` / `proc_exited`.

| Topic | Behavior |
| --- | --- |
| `name` | At most one **running** proc per name (else 409 `proc_exists` naming it). An exited / killed proc with the same name is removed (record + logs) before the new start. |
| Environment | Exactly `env`, plus each `inherit_env` name copied from the app's own environment when present (`env` wins on overlap), plus `CR_SANDBOX_PROC_ID=<proc_id>`. Nothing else is inherited — never `SANDBOX_API_TOKEN` unless passed explicitly. `CR_SANDBOX_PROC_ID` / `CR_SANDBOX_JOB_ID` are reserved. |
| `reset_dirs` | Removed recursively before `files` are written: engine homes under `/tmp` are tenant-writable, so wiping them stops a previous run from planting configuration (e.g. `$DSH_HOME/cordis.patch.yml`). Each entry must be a normalized absolute path, not `/` or a top-level directory (`/tmp`, `/workspace`, …), not a mount point and not containing one (`/workspace/work`, role directories), with no symlink among its parents; a symlink at the path itself is unlinked, never followed. Validation happens before any side effect. |
| `files` | Absolute paths; missing parents are created (0700); each file is written to a temporary file and renamed into place with `mode` (default 0600). |
| Process | Own session / process group (signals go to the whole group); `cwd` must exist (default: the terminal's initial directory). |
| stdin | `close: true` closes it after writing — pi and DSH treat EOF as an orderly shutdown. Repeated `close` is idempotent. |
| Logs | `/tmp/cr-procs/<proc_id>/stdout` and `stderr`; offsets start at 0 per stream and `stdout_size` / `stderr_size` count every byte ever written. `log_retain_bytes > 0` keeps the latest that many bytes of each stream readable (segments `stdout.<offset>`, older ones deleted); a cursor before that window returns data from the window start with `truncated: true`. `0` = unlimited. `max_bytes` (default 1 MiB, cap 16 MiB) bounds one read per stream. Logs are read and written through open descriptors, so deleting the files from inside the sandbox does not break a stream. |
| End | After the main process exits, remaining output is drained (up to 2 s if descendants still hold the pipes), then state, exit code and sizes are final. `killed` = it ended after a signal sent through `/signal` or `DELETE`; otherwise `exited`; a negative `exit_code` means death by that signal. A reader is done once `state != running` and both cursors reached the sizes. |
| Activity | Running procs count in `running_jobs` (and `running_procs`); live processes that carry the proc marker — including descendants that outlive their proc — count in `marked_processes`. Idle reclaim therefore never takes an instance with a live engine. Procs are not terminal jobs: the Hub does not tag them with a job origin and they are not in the job ledger, because one engine process spans many product turns. |

### Take a screenshot

```bash
curl -X POST http://localhost:8088/v1/sandboxes/sb_abc123/proxy/api/screen/screenshot
# → {"image": "<base64-png>", "width": 1024, "height": 768}
```

### Release a sandbox

```bash
curl -X POST http://localhost:8088/v1/sandboxes/sb_abc123/release
# → {"ok": true}
```

### List all sandboxes

```bash
curl http://localhost:8088/v1/sandboxes
```

---

## Sandbox Tool API

The Ubuntu container exposes 40+ REST endpoints and 30+ MCP tools. Key categories:

| Category | Endpoints | Description |
|----------|-----------|-------------|
| Terminal | `/api/terminal/execute`, `/wait`, `/kill`, `/restart`, `/execute/stream` | Job-based bash execution in per-conversation tmux sessions, long-poll, kill, SSE streaming |
| Procs | `/api/procs`, `/{id}/stdin`, `/{id}/read`, `/{id}/signal`, `DELETE /{id}` | Long-lived stdio processes (execution engines): explicit env, separate byte-exact stdout/stderr logs, cursor long-poll |
| Screen | `/api/screen/screenshot`, `/screenshot/region` | Full-screen or region capture |
| Mouse | `/api/mouse/click`, `/move`, `/drag`, `/scroll` | Pixel-level mouse control |
| Keyboard | `/api/keyboard/key`, `/type` | Key press, text input |
| File | `/api/file/view`, `/create`, `/replace`, `/insert` | File read/write/edit |
| Browser | `/api/browser/cdp/*` | Chrome DevTools Protocol — navigate, click, evaluate JS |
| System | `/api/system/health`, `/clipboard`, `/info` | Health check, clipboard, system info |
| Process | `/api/process/list`, `/kill` | Process management |

Full API docs available at `http://localhost:8000/docs` inside a running container.

---

## Configuration

### `.env`: deployment (host / network / object-storage connection / secrets)

| Variable | Default | Description |
|----------|---------|-------------|
| `SANDBOX_HUB_HOST` | `0.0.0.0` | Listen address; tighten to `127.0.0.1` or an intranet IP for private deployments |
| `SANDBOX_HUB_PORT` | `8088` | SandboxHub service port |
| `CONTAINER_LABEL` | `sandboxhub.managed` | Label on managed containers (isolates multiple instances on one host) |
| `SANDBOX_NETWORK` | `cr-sb-net` | Docker network for *online* containers. Must be a user-defined bridge network (SandboxHub creates it idempotently); with the built-in `bridge/host/none` the network policy is unavailable (an acquire asking for `deny` gets 400; `allow` behaves as before) |
| `SANDBOX_NETWORK_ISOLATED` | `cr-sb-isolated` | *Offline* network (`--internal`, created by SandboxHub): sandboxes with policy `deny` are hot-switched here and can only reach `cr-host` |
| `SANDBOX_GATEWAY_NAME` | `cr-host` | Gateway container name = the fixed hostname containers use for MinIO / the createrole API; resolvable on both networks |
| `SANDBOX_GATEWAY_FORWARDS` | _(empty)_ | Extra gateway port forwards, comma-separated `listen=host-reachable-address:port` (`host.docker.internal` = the host); the MinIO forward is added automatically from `MINIO_ENDPOINT`. Forward `8012` here when createrole uses `SANDBOX_MARKET_CLI_API_BASE=http://cr-host:8012` |
| `SANDBOX_HTTP_PROXY` | _(empty)_ | Egress proxy injected into ubuntu containers; a host proxy must use a container-reachable address (`host.docker.internal`) |
| `SANDBOX_DNS` | _(empty)_ | Custom container DNS (comma-separated, injected via `docker --dns`); when set, `SANDBOX_KEEP_DNS=1` is also injected so the ubuntu entrypoint does not overwrite `resolv.conf` |
| `SANDBOX_KEEP_DNS` | `false` | `true` = always inject `SANDBOX_KEEP_DNS=1` so containers keep the Docker-injected `resolv.conf` (host `daemon.json` / `--dns`). Needs a rebuilt image |
| `MINIO_ENDPOINT` | _(empty)_ | MinIO `host:port` (no scheme), **reachable from the host side** (`host.docker.internal:9000` or a host NIC IP): containers actually go through the gateway `cr-host:<port>`; only with the built-in `bridge` network must it be container-reachable (`172.17.0.1:9000`). Empty disables mounting |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | _(empty)_ | MinIO credentials passed inline to the container's rclone; must point at the same instance createrole uses |
| `MINIO_SECURE` | `false` | `true` = use https for MinIO |
| `SANDBOX_HUB_API_KEY` | _(empty)_ | Optional auth: when set, every request must carry a matching `X-API-Key` header (401 otherwise); `/v1/health` is exempt |

### `config/system.yaml`: system tuning (in git, changed via PR)

| Key | Default | Description |
|-----|---------|-------------|
| `image.ubuntu` / `image.code` | `sandbox-ubuntu:latest` / `sandbox-code:latest` | Image name per profile |
| `warm_pool.ubuntu` / `warm_pool.code` | `3` / `0` (this repo's live box: ubuntu=1) | Pre-warmed containers, 0 = cold start on acquire |
| `warm_pool.maintain_interval` | `30` | Seconds between pool replenishment checks |
| `sandbox.api_port` | `8000` | FastAPI port inside the container; matches the image entrypoint |
| `sandbox.idle_ttl` | `7200` | Reclaim only after request inactivity and confirmed absence of running jobs (seconds), 0 = off |
| `sandbox.file_upload_max_bytes` | `0` | Container file upload size cap in bytes, 0 = unlimited; applies to newly created containers |
| `reconcile.interval` | `60` | Reconciler period (seconds) |
| `reconcile.orphan_grace_seconds` | `300` | Grace period before an unregistered running container is destroyed |
| `proxy.read_timeout` / `proxy.connect_timeout` | `330` / `10` | Proxy timeouts (seconds); read timeout must exceed the longest single terminal request (legacy `timeout` cap 300s; job-contract `wait` cap 120s) |
| `workspace.mount_enabled` | `true` | Master switch for the MinIO workspace mount |
| `workspace.rclone_vfs_cache_mode` | `full` | rclone VFS cache mode; `full` avoids EIO on rename-over inside the write-back window (issue #9) |
| `workspace.rclone_vfs_cache_max_size` | `2G` | Local VFS cache eviction target, not a file/workspace quota; open or pending files can exceed it |
| `workspace.rclone_vfs_write_back` | `1s` | Delay before a closed file is uploaded to MinIO |
| `workspace.rclone_dir_cache_time` | `2s` | Directory listing cache (MinIO→container visibility lag) |
| `workspace.mount_ready_retries` / `mount_ready_interval` | `20` / `0.5` | Mountpoint readiness probe attempts and interval (seconds) |

System keys are declared in `SYSTEM_KNOBS` (`src/config.py`); `tests/unit/test_system_config.py` checks the yaml declares
exactly that set. Per-machine differences (image names / pool sizes on a GPU box vs. a dev box) go through a branch or PR, not env.

---

## Offline / Air-gapped Deployment Notes

For private deployments on customer machines without internet access. Build the sandbox
images on the target machine (or load them via `docker save`/`docker load`) **before**
going offline — build time requires internet, runtime does not.

**1. Container DNS.** The ubuntu image's entrypoint overwrites `/etc/resolv.conf` with
`8.8.8.8`/`1.1.1.1` at startup, which shadows any `docker --dns` configuration and makes
every in-container DNS lookup time out on an offline machine. Fix: set `SANDBOX_DNS` to
the customer's intranet DNS (or set `SANDBOX_KEEP_DNS=true` to rely on the host's Docker
DNS config); either one injects `SANDBOX_KEEP_DNS=1` into containers, which the
entrypoint honors by skipping the overwrite. **This requires an image rebuilt from the
current `entrypoint.sh`** — older images ignore the variable. The code image never
overwrites `resolv.conf`, so it needs nothing beyond `--dns`.

**2. In-sandbox `pip install` / `npm install`.** Both images bake in public Chinese
mirrors: pip is pointed at the Tsinghua PyPI mirror (`pip config set` during build, see
`images/ubuntu/Dockerfile` and `images/code/Dockerfile`) and npm/yarn/pnpm at
`registry.npmmirror.com` (`NPM_CONFIG_REGISTRY` env + `npm config set`). Offline, any
package installation inside a sandbox will fail. There is **no SandboxHub config knob**
to override these at runtime. Options if the customer has an internal mirror:
- Per command (works today, agent-driven): `pip install -i http://<mirror>/simple <pkg>`
  and `npm install --registry=http://<mirror> <pkg>`. Not persistent across containers.
- Permanent: rebuild the images with the mirror URLs replaced (the pip `config set` /
  `NPM_CONFIG_REGISTRY` lines in both Dockerfiles), or pre-install everything needed at
  build time.

**3. Lock down the API.** SandboxHub is an arbitrary-command-execution endpoint. On a
shared intranet, set `SANDBOX_HUB_API_KEY` (the createrole client sends the matching
`X-API-Key` header using the same-named env var) and/or bind `SANDBOX_HUB_HOST` to
`127.0.0.1` when co-located with createrole.

**4. MinIO endpoint.** `MINIO_ENDPOINT` is a *host-reachable* address (`host.docker.internal:9000`); containers reach MinIO through the gateway `cr-host:9000`. Only with the built-in `bridge` network must it be reachable *from inside containers*.
`172.17.0.1:9000` is the host-gateway address of the default `bridge` network; adjust it
for custom networks or a remote MinIO, and make sure it points at the **same MinIO
instance** createrole uses.

**5. A sandbox with no network at all: the network policy (SandboxHub#42).** Cutting the host's
uplink only removes NAT egress; containers can still reach the whole LAN and every host service
bound to `0.0.0.0` (Postgres, Redis, ...). For a truly offline sandbox use createrole's business
setting `sandbox.network_enabled` (hot-reloaded from the admin console, no restart): it is sent with
every acquire as `policy.network.default=deny`, and SandboxHub hot-switches the container onto the
`--internal` network `cr-sb-isolated` — no default route, no NAT, and on Docker ≥ 28 not even the host
is reachable (verified on Docker 29.2.1: container → host gateway port / LAN / internet / DNS all fail;
host → container and container → container on the same network work). The container can then only
reach the gateway container `cr-host` (`images/gateway`, alpine + socat), attached to both networks,
which forwards exactly two platform channels to the host: MinIO (dropped once FUSE goes away) and the
createrole API (memory/todo CLI). Deployment: `SANDBOX_NETWORK` must be a user-defined network
(default `cr-sb-net`; delete any old `SANDBOX_NETWORK=bridge` from `.env`), `MINIO_ENDPOINT` becomes a
host-reachable address, createrole's `SANDBOX_MARKET_CLI_API_BASE` becomes `http://cr-host:<port>` with
that port listed in `SANDBOX_GATEWAY_FORWARDS`, and the gateway image is built / imported with the
others (`scripts/build-images.sh gateway`). Optional belt-and-braces for Docker < 28: a host firewall rule
dropping new inbound connections from the isolated subnet. Acceptance inside a `deny` container: LAN /
internet / DNS all fail, `curl http://cr-host:9000/minio/health/live` succeeds; a `sleep 600` tmux job
survives a `policy/apply` deny → allow round trip (one switch takes ~0.3–0.4 s). Known limitation (unchanged):
sandboxes on the same network can reach each other. Policy shape and the `policy/apply` endpoint are
documented under *Acquire a sandbox* in the Chinese README.

**6. Tell the agent it is offline.** Nothing in the container tells the model
whether egress works. Offline, the model only sees raw DNS/timeout errors and keeps
retrying `pip install` / `curl` in different spellings. The same setting
`sandbox.network_enabled=false` makes createrole add a one-line "sandbox
has no internet" notice, terminal results whose output looks like a network failure get a
"will always fail, do not retry, use what is preinstalled" hint, and the agent can read
the preinstalled inventory on demand. The `code` image ships that inventory as
`/etc/sandbox/MANIFEST.md`, generated at build time (`pip list`, `npm ls -g`, which CLI
tools exist / are missing, Python/Node versions) — the hand-written summary in
createrole's `me/SANDBOX.md` points to it; when the two disagree, the manifest is right.
Also remove the `web-composite-search` skill from role workspaces before hand-over (it
hits public search engines from inside the sandbox).

---

## Project Structure

```
SandboxHub/
├── main.py                    # Entry point — python main.py
├── src/                       # Orchestrator
│   ├── config.py
│   ├── main.py                # FastAPI app
│   ├── manager/
│   │   ├── container_manager.py
│   │   ├── registry.py        # (user_id, role_id) → container mapping
│   │   └── warm_pool.py       # Pre-warmed container pool
│   ├── proxy/
│   │   └── forwarder.py       # HTTP proxy to containers
│   └── routers/
│       ├── sandboxes.py       # acquire / release / status
│       └── proxy.py           # /v1/sandboxes/{id}/proxy/*
├── images/
│   └── ubuntu/
│       ├── Dockerfile         # Multi-arch (amd64 + arm64)
│       ├── scripts/           # Container startup scripts
│       │   ├── entrypoint.sh
│       │   └── start_all.sh
│       └── app/               # FastAPI + MCP app (runs inside container)
│           ├── main.py
│           ├── mcp_server.py
│           ├── routers/       # 9 tool routers
│           └── tools/         # BashTool, ComputerTool, EditTool
├── tests/                     # Orchestrator tests
└── images/ubuntu/tests/       # Sandbox app tests
```

---

## Adding a New Sandbox Type

1. Add a new image directory: `images/<type>/Dockerfile`
2. Register in `src/config.py`:
   ```python
   def image_for_type(self, sandbox_type: str) -> str:
       mapping = {
           "ubuntu": self.DOCKER_IMAGE_UBUNTU,
           "debian": self.DOCKER_IMAGE_DEBIAN,   # new
       }
   ```
3. Declare `DOCKER_IMAGE_<TYPE>` / `WARM_POOL_<TYPE>` fields in `src/config.py`, add them to `SYSTEM_KNOBS`, and set the values under `image` / `warm_pool` in `config/system.yaml` (the reconciliation test enforces this)
4. No changes needed to Registry, Router, or Proxy

---

## Development

```bash
# Run orchestrator tests
pytest tests/ -v

# Run sandbox app tests
PYTHONPATH=images/ubuntu pytest images/ubuntu/tests/ -v

# Build image for a specific architecture
docker build --platform linux/amd64 -t sandbox-ubuntu:latest images/ubuntu/

# Run a sandbox container directly (without SandboxHub)
docker run -d --name sandbox --shm-size=2g \
  -p 8000:8000 -p 8001:8001 -p 6080:6080 -p 5900:5900 \
  sandbox-ubuntu:latest
```

---

## Architecture Notes

**Warm pool** pre-creates containers in the background so `acquire` returns in milliseconds. The pool maintainer runs every 30s to replenish containers consumed by allocations.

**Graceful shutdown** drains all containers (both warm pool and allocated) before exit, ensuring no orphaned Docker containers.

**Terminal jobs** run as tmux windows (`script -q -f -e` gives each command a PTY and records it to the job log); completion is detected by polling `pane_dead`, the exit code comes from the wrapper's EXIT trap (`pane_dead_status` is only a fallback — tmux 3.2a occasionally never fills it). The streaming variant (`execute_stream`) tails the job log line-by-line.

**VLM vs LLM routing**: The sandbox supports both modalities. LLMs should use terminal/CDP endpoints (low token cost). VLMs can use screenshot + mouse/keyboard for pixel-level interaction.

## Work-area v2 (ADR 0025)

v2 allocates compute by `work_area_id`. A work area persists independently of its
container; releasing a container preserves `work/` and `deliverables/`. The v1
examples above describe the legacy protocol, available until the file-space cutover.

The host owns privileged rclone mounts through the `cr-fs` sidecar. Tenant
containers receive no storage credentials, FUSE device or mount capability. The
`/workspace` root is a read-only skeleton mounted before its children; cwd is
`/workspace/work`. `inputs/` and `context/` are read-only views, work and deliverables
are writable, and each role directory has the requested access mode. Every acquire
returns the effective layout, mounts, policy and isolation report. A failed self-test
rejects allocation without falling back to v1.

| Endpoint | Purpose |
| --- | --- |
| `GET /v2/capabilities?refresh=true` | Refresh isolation checks and return limits, ready image types, and per-type `procs` / `engines` |
| `POST /v2/work-areas/{id}/acquire` | Allocate/reuse an instance; optional inputs and context replace views |
| `GET /v2/sandboxes` | Registered instances with owner/work/session/turn metadata |
| `PUT /v2/sandboxes/{id}/inputs` | Update live inputs in place; removed grants disappear immediately |
| `PUT /v2/sandboxes/{id}/context` | Update generated instructions |
| `GET /v2/sandboxes/{id}/activity` | Known activity and running jobs (`running_jobs` includes running procs; `running_procs` separately, `null` for images without procs) |
| `POST /v2/sandboxes/{id}/quiesce` | Stop all tenant processes, preserving mounts for archive reads |
| `POST /v2/sandboxes/{id}/release` | Remove a running or quiesced instance, preserving the work area |
| `/v2/sandboxes/{id}/proxy/{path}` | Forward tool requests with the instance's API token |
| `POST /v2/sandboxes/policy/apply` | Broadcast effective network policy to registered v2 instances |
| `GET /v2/work-areas/{id}/usage` | Usage plus soft cap and `over_soft_cap` |
| `GET /v2/work-areas/{id}/files?root=deliverables` | Bounded listing, reporting truncated or skipped entries; `observed_at` is the Hub clock taken before listing |
| `GET /v2/work-areas/{id}/jobs` | Origin `(session_id, turn_id)` and start/end time of terminal jobs; `known=false` when completeness cannot be guaranteed |
| `GET /v2/work-areas/{id}/files/content?root=deliverables&path=...` | Host snapshot, refusing symlinks and path escape |
| `POST /v2/work-areas/{id}/files/checksums?root=work` | SHA-256 and stability for 1–128 paths (`{"paths": [...]}`); missing files reported individually, symlinks refused |
| `POST /v2/work-areas/{id}/import?root=work` | Safe tar import for migration |
| `DELETE /v2/work-areas/{id}?force=true` | Explicit removal; the platform must complete archiving first |
| `GET/POST /v2/admin/drain` | Inspect/pause v1 acquisition and warm pool, wait for old jobs and drain |
| `POST /v2/admin/files-cutover` | After drain, persist v2-only state and remove MinIO gateway forwarding |
| `DELETE /v2/admin/drain` | Restore v1 and configured gateway forwarding during rollback |

v2 requires the deployment Hub API key (`X-API-Key`); tool APIs inside each tenant
require a distinct `X-Sandbox-Token`. Content responses include `X-File-Size`,
`X-File-Mtime-Ns`, `X-File-Sha256` and `X-File-Stable`. Files can be read after
quiesce or release; a quiesced container is rebuilt on the next acquire.

The host persists the latest session and turn that acquired each instance in
`origin.json`, outside tenant mounts, and restores it after restart. Missing,
invalid or mismatched origin data yields an unknown source; archive callers
retain the user asset without guessing which conversation produced it.

Terminal jobs started through the proxy (`api/terminal/execute`, `execute/stream`,
including jobs that later continue in the background) are tagged with the instance's
current session and turn at request time. The Hub passes them in the trusted
`X-Sandbox-Job-Origin` header (callers' copies are dropped); the container stores the
origin with the job and reports it through terminal activity. Every activity query
(platform idle checks, `/jobs`, reconcile for instances with unobserved starts or open
jobs, and a final probe before quiesce/release) merges the report into
`WORK_AREA_ROOT/job-ledger/<work_area_id>.json`, so records survive instance rebuilds
and Hub restarts. `GET /v2/work-areas/{id}/jobs` returns:

```json
{"known": true, "now": "2026-10-22T08:00:00.123456Z",
 "jobs": [{"job_id": "j_…", "session_id": "…", "turn_id": "…",
           "started_at": "2026-10-22T07:59:00.000000Z", "ended_at": null}]}
```

All times are ISO-8601 UTC with `Z` on the host clock (containers share the host
kernel's real-time clock). `ended_at` is an upper bound; `null` means still running.
Jobs still open when their instance is stopped or removed end at that moment.
`known=false` when the live instance cannot be probed or runs an image without the
ledger, a live job has no origin, an instance vanished after terminal starts that were
never observed, or the ledger file is unreadable; the last three keep the ledger
unknown until they age past `job_max_lifetime` (one day when 0). Ended records are
removed after the same period. A missing work area returns
`404 {"detail": {"reason": "work_area_not_found", ...}}`.

**Execution engines (SandboxHub#52).** Engine processes run through the proxy as
`/api/procs` (see *Long-lived processes* above). They are deliberately **not** in
`TERMINAL_START_PATHS`: one engine process serves many product turns, so tagging it with the
turn that started it would attribute every later turn's output to that first turn, and the
job ledger above stays terminal-jobs-only. `GET /v2/capabilities` reports, for each configured
v2 image (refreshed with every self-test; a missing image or label reads as unsupported):

```json
{"procs":   {"ubuntu": true, "code": true},
 "engines": {"ubuntu": {}, "code": {"pi": "1.4.0", "dsh": "0.2.1-rc.3"}}}
```

from the image labels `sandboxhub.procs="1"` and `sandboxhub.engines` (JSON); all other
fields are unchanged. Running procs count as activity, so neither the platform's idle release
nor the v1 reaper reclaims an instance with a live engine, and the engine supervisor's
long-polls through the proxy (≤ 120 s each, under the 330 s proxy read timeout) keep the
instance's API traffic flowing, so `idle_backstop_ttl` only removes it once the platform has
been gone that long. v1 warm containers delete leftover procs during their reset before
returning to the pool. Engines reach the platform (model-gateway proxy, platform MCP) at the
same address the sandbox CLIs use, the gateway host alias and forwarded port in
`SANDBOX_MARKET_CLI_API_BASE` (e.g. `http://cr-host:8013`): the v2 gateway is attached to both
v2 networks under the `SANDBOX_GATEWAY_NAME` alias and forwards `SANDBOX_GATEWAY_FORWARDS`,
and netguard admits instance → gateway and gateway → host on both, so isolated (`deny`)
instances keep that path and no extra egress rule is needed.

Resource settings belong to `config/system.yaml`, under `work_area`:

| Key | Default | Behavior |
| --- | --- | --- |
| `max_per_role` | 2 | Concurrent instances per digital person |
| `max_per_user` | 4 | Concurrent instances per owner |
| `idle_release_seconds` | 900 | Platform idle reclaim threshold, advertised in capabilities |
| `soft_cap_bytes` | 10737418240 | 10 GiB; platform checks before file writes, not a hard filesystem quota |
| `job_max_lifetime` | 21600 | Kill a background terminal job after 6 hours, `kill_reason=max_lifetime` (procs are not capped) |
| `idle_backstop_ttl` | 86400 | Hub backstop for instances without API traffic; work areas remain |

Both v2 networks deny peer access and unapproved host ports. v2 gateway forwarding
always excludes MinIO, including explicit manual forwards to its endpoint/port.
After a verified file-space migration, cutover also removes legacy MinIO forwarding
and persists `.files-cutover.json` under `WORK_AREA_ROOT`. A Hub restart retains
v1 drain, warm-pool pause and forwarding restrictions. Rollback clears this state,
releases v2 instances while retaining their volumes, and restores the configured v1
behavior. Drain does not force-kill busy old jobs unless explicitly requested.

Build with version tags (`NO_LATEST=1 bash scripts/build-images.sh all`). Real
isolation tests use temporary networks, sidecar, gateway and a dedicated test bucket:
`SANDBOXHUB_LIVE=1 python -m pytest tests/integration/test_work_area_live.py` with
MinIO credentials supplied by the environment. They do not restart existing Hubs.
