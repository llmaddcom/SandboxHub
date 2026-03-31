# SandboxHub

一个自托管的沙盒编排服务，为 LLM/VLM Agent 管理隔离的 Docker 容器。架构灵感来源于 [Anthropic Claude Computer Use 演示](https://github.com/anthropics/claude-quickstarts/tree/main/computer-use-demo)，在其基础上新增了预热池编排层、多沙盒管理和实时终端流式输出。

> English documentation: [README.md](./README.md)

---

## 概述

SandboxHub 包含两个组件，共同维护在本 monorepo 中：

| 组件 | 路径 | 职责 |
|------|------|------|
| **编排层** | `src/` | 管理容器生命周期 — 预热池、acquire/release、HTTP 代理 |
| **Ubuntu 镜像** | `images/ubuntu/` | Ubuntu 22.04 沙盒 — 虚拟桌面、FastAPI 工具接口、MCP 服务 |

```
LLM Agent
    │  POST /v1/sandboxes/acquire
    ▼
SandboxHub :8088  ─── 预热池 ──→  Ubuntu 容器
    │                                   ├─ FastAPI  :8000  (40+ REST 工具)
    │  代理 /v1/sandboxes/{id}/proxy/   ├─ FastMCP  :8001  (30+ MCP 工具)
    ▼                                   ├─ noVNC    :6080  (网页桌面)
  响应                                  └─ VNC      :5900
```

### 与 Claude Computer Use 的关系

Ubuntu 沙盒镜像直接参考了 Anthropic 的 [computer-use-demo](https://github.com/anthropics/claude-quickstarts/tree/main/computer-use-demo)。延续的核心设计模式：

- **`BashSession` PTY 模式** — 持久化 bash 子进程，通过哨兵字符串检测命令完成（`images/ubuntu/app/tools/bash.py`）
- **`ToolResult` / `CLIResult` 抽象** — 结构化工具输出，便于 LLM 消费
- **虚拟桌面栈** — TigerVNC + openbox + noVNC，支持 VLM 截图点击工作流
- **lifespan 工具注入** — 启动时将 `BashTool`、`ComputerTool`、`EditTool` 单例注入各 FastAPI 路由

SandboxHub 在此基础上新增：
- **预热池（Warm Pool）** — 预先创建容器，消除冷启动，acquire 延迟 <100ms
- **注册表（Registry）** — 按 `(user_id, role_id)` 跟踪已分配容器，支持复用
- **HTTP 代理层** — 统一入口，将所有工具调用路由到对应容器
- **SSE 流式输出** — `POST /api/terminal/execute/stream` 实时推送 stdout（扩展原始轮询模型）
- **多架构 Dockerfile** — 同时支持 amd64（Google Chrome）和 arm64（Chromium）

---

## 快速开始

### 1. 构建沙盒镜像

```bash
docker build -t sandbox-ubuntu:latest images/ubuntu/
```

> **国内网络说明：** Dockerfile 已配置阿里云 APT 镜像、TUNA pip 镜像，以及 noVNC/pyenv 的 Gitee 镜像，构建无需代理。

### 2. 安装并配置 SandboxHub

```bash
pip install -e .

cp .env.example .env   # 按需编辑
```

主要 `.env` 配置项：

```env
DOCKER_IMAGE_UBUNTU=sandbox-ubuntu:latest
WARM_POOL_UBUNTU=3
SANDBOX_HUB_PORT=8088
```

### 3. 启动 SandboxHub

```bash
python main.py
```

或直接用 uvicorn：

```bash
uvicorn src.main:app --host 0.0.0.0 --port 8088 --reload
```

### 4. 健康检查

```bash
curl http://localhost:8088/v1/health
# {"ok": true, "warm_pool": {"ubuntu": {"available": 3, "allocated": 0}}}
```

---

## 接口

### 申请沙盒

```bash
curl -X POST http://localhost:8088/v1/sandboxes/acquire \
  -H "Content-Type: application/json" \
  -d '{"user_id": "u1", "role_id": "r1", "sandbox_type": "ubuntu"}'
# → {"sandbox_id": "sb_abc123", "status": "ready"}
```

从预热池返回，耗时 <100ms。若相同 `(user_id, role_id)` 已有容器分配，则直接复用。

### 执行终端命令

```bash
curl -X POST http://localhost:8088/v1/sandboxes/sb_abc123/proxy/api/terminal/execute \
  -H "Content-Type: application/json" \
  -d '{"command": "ls /workspace", "timeout": 30}'
# → {"success": true, "output": "...", "error": null}
```

### 流式终端输出（SSE）

```bash
curl -X POST http://localhost:8088/v1/sandboxes/sb_abc123/proxy/api/terminal/execute/stream \
  -H "Content-Type: application/json" \
  -d '{"command": "python train.py"}' \
  --no-buffer
# data: {"type": "stdout", "chunk": "Epoch 1/10\n"}
# data: {"type": "stdout", "chunk": "loss: 0.42\n"}
# data: {"type": "done"}
```

### 截图

```bash
curl -X POST http://localhost:8088/v1/sandboxes/sb_abc123/proxy/api/screen/screenshot
# → {"image": "<base64-png>", "width": 1024, "height": 768}
```

### 释放沙盒

```bash
curl -X POST http://localhost:8088/v1/sandboxes/sb_abc123/release
# → {"ok": true}
```

### 查看所有沙盒

```bash
curl http://localhost:8088/v1/sandboxes
```

---

## 沙盒工具接口

Ubuntu 容器对外暴露 40+ REST 接口和 30+ MCP 工具，主要分类：

| 分类 | 接口 | 说明 |
|------|------|------|
| 终端 | `/api/terminal/execute`、`/execute/stream` | bash 命令、PTY 会话、SSE 流式输出 |
| 屏幕 | `/api/screen/screenshot`、`/screenshot/region` | 全屏或区域截图 |
| 鼠标 | `/api/mouse/click`、`/move`、`/drag`、`/scroll` | 像素级鼠标控制 |
| 键盘 | `/api/keyboard/key`、`/type` | 按键、文本输入 |
| 文件 | `/api/file/view`、`/create`、`/replace`、`/insert` | 文件读写编辑 |
| 浏览器 | `/api/browser/cdp/*` | Chrome DevTools 协议 — 导航、点击、执行 JS |
| 系统 | `/api/system/health`、`/clipboard`、`/info` | 健康检查、剪贴板、系统信息 |
| 进程 | `/api/process/list`、`/kill` | 进程管理 |

容器运行后，完整 API 文档见 `http://localhost:8000/docs`。

---

## 配置项

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SANDBOX_HUB_PORT` | `8088` | SandboxHub 服务端口 |
| `DOCKER_IMAGE_UBUNTU` | `sandbox-ubuntu:latest` | Ubuntu 沙盒镜像名 |
| `WARM_POOL_UBUNTU` | `3` | 预热 Ubuntu 容器数量 |
| `SANDBOX_NETWORK` | `bridge` | Docker 网络模式 |
| `SANDBOX_API_PORT` | `8000` | 容器内 FastAPI 端口 |
| `POOL_MAINTAIN_INTERVAL` | `30` | 池补充检查间隔（秒） |
| `SANDBOX_HTTP_PROXY` | （空）| 注入容器的 HTTP 代理 |

---

## 项目结构

```
SandboxHub/
├── main.py                    # 启动入口 — python main.py
├── src/                       # 编排层
│   ├── config.py
│   ├── main.py                # FastAPI 应用
│   ├── manager/
│   │   ├── container_manager.py
│   │   ├── registry.py        # (user_id, role_id) → 容器映射
│   │   └── warm_pool.py       # 预热容器池
│   ├── proxy/
│   │   └── forwarder.py       # HTTP 代理转发
│   └── routers/
│       ├── sandboxes.py       # acquire / release / status
│       └── proxy.py           # /v1/sandboxes/{id}/proxy/*
├── images/
│   └── ubuntu/
│       ├── Dockerfile         # 多架构（amd64 + arm64）
│       ├── scripts/           # 容器启动脚本
│       │   ├── entrypoint.sh
│       │   └── start_all.sh
│       └── app/               # 容器内 FastAPI + MCP 应用
│           ├── main.py
│           ├── mcp_server.py
│           ├── routers/       # 9 个工具路由
│           └── tools/         # BashTool、ComputerTool、EditTool
├── tests/                     # 编排层测试
└── images/ubuntu/tests/       # 沙盒应用测试
```

---

## 扩展新沙盒类型

1. 添加新镜像目录：`images/<type>/Dockerfile`
2. 在 `src/config.py` 中注册：
   ```python
   def image_for_type(self, sandbox_type: str) -> str:
       mapping = {
           "ubuntu": self.DOCKER_IMAGE_UBUNTU,
           "debian": self.DOCKER_IMAGE_DEBIAN,   # 新增
       }
   ```
3. 在 `.env` 中添加 `WARM_POOL_<TYPE>=N`
4. Registry、Router、Proxy 无需改动

---

## 开发

```bash
# 运行编排层测试
pytest tests/ -v

# 运行沙盒应用测试
PYTHONPATH=images/ubuntu pytest images/ubuntu/tests/ -v

# 构建特定架构镜像
docker build --platform linux/amd64 -t sandbox-ubuntu:latest images/ubuntu/

# 直接运行沙盒容器（不经过 SandboxHub）
docker run -d --name sandbox --shm-size=2g \
  -p 8000:8000 -p 8001:8001 -p 6080:6080 -p 5900:5900 \
  sandbox-ubuntu:latest
```

---

## 架构说明

**预热池（Warm Pool）** 在后台预先创建容器，使 `acquire` 可在毫秒内返回。池维护任务每 30 秒运行一次，补充因分配消耗的容器。

**优雅退出** 会在进程退出前清理所有容器（预热池 + 已分配），确保不留孤儿容器。

**BashSession** 使用持久化 PTY 和 UUID 哨兵字符串检测命令完成。流式变体 `run_stream` 通过 `asyncio.readline()` 逐行 yield stdout，支持长时间运行命令的实时输出。

**VLM vs LLM 接口选择**：沙盒同时支持两种模态。LLM 应优先使用终端和 CDP 接口（token 消耗极低）；VLM 可使用截图 + 鼠标/键盘进行像素级交互。
