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
- **SSE streaming** — `POST /api/terminal/execute/stream` streams stdout in real-time (extends the original polling model)
- **Multi-arch Dockerfile** — builds on both amd64 (Google Chrome) and arm64 (Chromium)

---

## Quick Start

### 1. Build the sandbox image

```bash
docker build -t sandbox-ubuntu:latest images/ubuntu/
```

> **Network note (China):** The Dockerfile uses Aliyun APT mirrors, TUNA pip mirrors, and Gitee mirrors for noVNC/pyenv. No VPN needed for the build.

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
| `DOCKER_IMAGE_UBUNTU` | `sandbox-ubuntu:latest` | Ubuntu sandbox image name |
| `WARM_POOL_UBUNTU` | `3` | Pre-warmed Ubuntu containers |
| `SANDBOX_NETWORK` | `bridge` | Docker network mode |
| `SANDBOX_API_PORT` | `8000` | Port exposed by the container's FastAPI |
| `POOL_MAINTAIN_INTERVAL` | `30` | Seconds between pool replenishment checks |
| `SANDBOX_HTTP_PROXY` | _(empty)_ | HTTP proxy injected into containers |

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
