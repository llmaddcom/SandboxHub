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

- **`BashSession` PTY pattern** — persistent bash subprocess with sentinel-based command completion detection (`images/ubuntu/app/tools/bash.py`)
- **`ToolResult` / `CLIResult` abstractions** — structured tool output for LLM consumption
- **Virtual desktop stack** — TigerVNC + openbox + noVNC for VLM screenshot-and-click workflows
- **Tool injection via lifespan** — `BashTool`, `ComputerTool`, `EditTool` singletons injected into FastAPI routers at startup

SandboxHub adds on top:
- **Warm pool** — pre-warmed containers eliminate cold-start latency (<100ms acquire)
- **Registry** — tracks allocated containers per `(user_id, role_id)` pair, enables reuse
- **HTTP proxy layer** — single ingress point; routes all tool calls to the right container
- **Reconciler** — self-healing lifecycle: health-check on acquire (dead sandboxes evicted and transparently re-created), startup recovery after host/service restarts (stopped containers removed, leftover warm containers reset then re-adopted), periodic sweep that destroys untracked orphan containers and auto-reclaims idle sandboxes
- **SSE streaming** — `POST /api/terminal/execute/stream` streams stdout in real-time (extends the original polling model)
- **Multi-arch Dockerfile** — builds on both amd64 (Google Chrome) and arm64 (Chromium)

---

## Quick Start

### 1. Build the sandbox image

Preferred: the build script (tags both `latest` and the version from `images/ubuntu/app/VERSION`):

```bash
scripts/build-images.sh          # build code + ubuntu
scripts/build-images.sh code     # code only
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

> **Code image toolchain:** Python 3.11 + Node 20 (yarn/pnpm), `git`/`ripgrep`/`jq`/`vim`, build-essential, and daily Python libs (pandas, openpyxl, python-docx/pptx, reportlab, pypdf, matplotlib, markitdown…). Agents can introspect it at runtime via `GET /api/system/env`.

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

cp .env.example .env   # edit as needed
```

Key `.env` settings:

```env
DOCKER_IMAGE_UBUNTU=sandbox-ubuntu:latest
WARM_POOL_UBUNTU=3
SANDBOX_HUB_PORT=8088
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
(e.g. for in-sandbox CLIs like `skillhub` / `todo` calling back to the backend). Semantics:

- **Creation-time only** — ignored when reusing an existing sandbox for the same
  `(user_id, role_id)` (callers use sliding-renewal tokens, so the initially injected value stays valid);
- **Bypasses the warm pool** — pooled containers were created without these vars and Docker
  cannot inject env into a running container, so a first allocation with `env` cold-starts;
- **Tenant-dedicated** — values may be credentials: the container is destroyed on release
  (never returned to the shared pool), and logs record env keys only, never values;
- Omitting `env` keeps the exact current behavior.

### Execute a terminal command

```bash
curl -X POST http://localhost:8088/v1/sandboxes/sb_abc123/proxy/api/terminal/execute \
  -H "Content-Type: application/json" \
  -d '{"command": "ls /workspace", "timeout": 30}'
# → {"success": true, "output": "...", "error": null}
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
| Terminal | `/api/terminal/execute`, `/execute/stream` | Bash commands, PTY session, SSE streaming |
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

| Variable | Default | Description |
|----------|---------|-------------|
| `SANDBOX_HUB_PORT` | `8088` | SandboxHub service port |
| `SANDBOX_HUB_HOST` | `0.0.0.0` | Listen address; tighten to `127.0.0.1` or an intranet IP for private deployments |
| `SANDBOX_HUB_API_KEY` | _(empty)_ | Optional API auth: when set, every request must carry a matching `X-API-Key` header (401 otherwise); `/v1/health` is exempt. Empty = no auth (unchanged behavior) |
| `SANDBOX_DNS` | _(empty)_ | Custom container DNS (comma-separated, injected via `docker --dns`); when set, `SANDBOX_KEEP_DNS=1` is also injected into containers so the ubuntu entrypoint does not overwrite `resolv.conf` |
| `SANDBOX_KEEP_DNS` | `false` | `true` = always inject `SANDBOX_KEEP_DNS=1` so containers keep the Docker-injected `resolv.conf` (host `daemon.json` / `--dns`) instead of the ubuntu entrypoint overwriting it with `8.8.8.8`/`1.1.1.1`. Needs a rebuilt image to take effect |
| `DOCKER_IMAGE_UBUNTU` | `sandbox-ubuntu:latest` | Ubuntu sandbox image name |
| `WARM_POOL_UBUNTU` | `3` | Pre-warmed Ubuntu containers |
| `SANDBOX_NETWORK` | `bridge` | Docker network mode |
| `SANDBOX_API_PORT` | `8000` | Port exposed by the container's FastAPI |
| `POOL_MAINTAIN_INTERVAL` | `30` | Seconds between pool replenishment checks |
| `SANDBOX_HTTP_PROXY` | _(empty)_ | HTTP proxy injected into containers |
| `WORKSPACE_MOUNT_ENABLED` | `true` | Master switch for the MinIO workspace mount feature |
| `MINIO_ENDPOINT` | _(empty)_ | MinIO `host:port` (no scheme) for the rclone S3 remote; empty disables mounting |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | _(empty)_ | MinIO credentials passed to the container's rclone |
| `MINIO_SECURE` | `false` | `true` = use https for MinIO |
| `RCLONE_VFS_CACHE_MODE` | `full` | rclone VFS cache mode; `full` avoids EIO on rename-over (`sed -i`) inside the write-back window (issue #9) |
| `RCLONE_VFS_CACHE_MAX_SIZE` | `2G` | Local VFS cache size cap (reads are cached in `full` mode) |
| `RCLONE_VFS_WRITE_BACK` | `1s` | Delay before a closed file is uploaded to MinIO |
| `RCLONE_DIR_CACHE_TIME` | `2s` | Directory listing cache (MinIO→container visibility lag) |

See `.env.example` for the full annotated list (reconciler, proxy timeouts, mount probing, etc.).

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

**4. MinIO endpoint.** `MINIO_ENDPOINT` must be reachable *from inside containers*.
`172.17.0.1:9000` is the host-gateway address of the default `bridge` network; adjust it
for custom networks or a remote MinIO, and make sure it points at the **same MinIO
instance** createrole uses.

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
3. Add `WARM_POOL_<TYPE>=N` to `.env`
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

**BashSession** uses a persistent PTY with UUID-based sentinels to detect command completion. The streaming variant (`run_stream`) yields stdout line-by-line via `asyncio.readline()`, enabling real-time output for long-running commands.

**VLM vs LLM routing**: The sandbox supports both modalities. LLMs should use terminal/CDP endpoints (low token cost). VLMs can use screenshot + mouse/keyboard for pixel-level interaction.
