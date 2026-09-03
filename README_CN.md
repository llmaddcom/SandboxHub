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
| **Code 镜像** | `images/code/` | 无 GUI 轻量沙盒 — Python + Node 工具链、开发/办公库、playwright headless chromium，仅终端/文件/系统/进程接口，秒级冷启动 |

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
- **对账器（Reconciler）** — 自愈式生命周期管理：acquire 复用前体检（死沙盒驱逐并透明重建）、宿主/服务重启后的启动恢复（清理已停止容器、遗留 warm 容器复位后收养回池）、周期对账（销毁不在册的孤儿容器、闲置沙盒自动回收）
- **job 化终端** — `execute(wait)` / `wait(cursor)` / `kill`：命令作为 job 在持久会话里跑（cwd / 导出环境跨调用保留），调用方分段长轮询取结果，无默认超时、无上限（issue #30）；全量输出落 `/tmp/cr-jobs/<job_id>.log`
- **SSE 流式输出** — `POST /api/terminal/execute/stream` 实时推送 stdout（扩展原始轮询模型）
- **多架构 Dockerfile** — 同时支持 amd64（Google Chrome）和 arm64（Chromium）

---

## 快速开始

### 1. 构建沙盒镜像

推荐用构建脚本（同时打 `latest` 与版本标签，版本取自 `images/ubuntu/app/VERSION`）：

```bash
scripts/build-images.sh          # 构建 code + ubuntu
scripts/build-images.sh code     # 只构建 code
```

也可手动构建：

```bash
# 完整桌面镜像（GUI + 浏览器 + 技能）
docker build -t sandbox-ubuntu:latest images/ubuntu/

# 轻量无 GUI 的 code 镜像（Python + Node + 开发/办公库）
# 构建上下文为 images/（复用 ubuntu/app 代码），故必须 -f 指定 Dockerfile 并以 images 为上下文：
docker build -f images/code/Dockerfile -t sandbox-code:latest images
```

> **镜像版本对账（防部署漂移）：** 改动 `images/` 下的 app 代码时请同步更新 `images/ubuntu/app/VERSION` 并重建镜像。
> 容器经 `GET /api/system/health` 上报自身 `app_version`，SandboxHub 的 reconciler 周期比对该版本与仓库
> `images/ubuntu/app/VERSION`，不一致时输出 `镜像版本漂移` 告警——「代码已合、镜像未重建」不再静默（issue #6）。

> **国内网络说明：** 两个 Dockerfile 均已配置国内镜像 —— APT/pip 用 TUNA、Node/npm 用 npmmirror，APT/pip/npm 安装无需代理。
> **code** 镜像可在墙内全程无代理构建；**ubuntu** 镜像还需从境外拉取以下资源（建议代理）：
> - Google Chrome / Chromium（arm64）
> - noVNC、websockify（GitHub）
> - pyenv（GitHub）

> **Code 镜像工具链：** Python 3.11 + Node 20（yarn/pnpm）、`git`/`ripgrep`/`jq`/`vim`、build-essential，以及日常 Python 库（pandas、openpyxl、python-docx/pptx、reportlab、pypdf、matplotlib、markitdown…）。Agent 可在运行时通过 `GET /api/system/env` 自检环境。

#### 使用代理构建

若本机已运行 v2ray/clash 等代理（HTTP 代理监听 `127.0.0.1:8118`），使用 `--network host` 让构建容器直接访问宿主机代理：

```bash
# 代理配置（按实际调整）
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

> `--network host` 使构建阶段的 `RUN` 命令与宿主机共享网络栈，从而能访问 `127.0.0.1` 上监听的本地代理。

### 2. 安装并配置 SandboxHub

```bash
pip install -e .

cp .env.example .env   # 只填连接 / 密钥 / 本机项
```

配置分两个家（issue #28，对齐 createrole#400）：

- `.env`（不进 git）：本机、网络、MinIO 连接、API Key——新部署只需填这一份。
- `config/system.yaml`（进 git，改动走 PR）：镜像名、预热池、对账回收、代理超时、rclone 挂载策略。
  取值 文件 > 代码默认；缺键 / 坏值仅该项回退并告警；**没有 env 覆盖口**，同名旧 ENV 启动期告警并忽略。

```env
SANDBOX_HUB_PORT=8088
MINIO_ENDPOINT=172.17.0.1:9000
MINIO_ACCESS_KEY=...
MINIO_SECRET_KEY=...
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

#### 携带环境变量注入（issue #15/#16）

```bash
curl -X POST http://localhost:8088/v1/sandboxes/acquire \
  -H "Content-Type: application/json" \
  -d '{"user_id": "u1", "role_id": "r1", "sandbox_type": "code",
       "env": {"CR_API_BASE": "http://host.docker.internal:8011", "CR_SANDBOX_TOKEN": "..."}}'
```

`env` 里的键值对在容器创建时注入为环境变量（供 `skills` / `todo` 等沙盒内 CLI
回连后端）。语义约束：

- **仅创建时生效**：复用同 `(user_id, role_id)` 已有沙盒时忽略该字段（调用方 token
  滑动续期，首次注入的值持续有效）；
- **绕过预热池**：池内容器创建时没有这些 env，Docker 无法向运行中容器补注入，
  故带 `env` 的首次分配走冷启动；
- **专属化**：值可能是租户凭据，该容器 release 即销毁、不归还共享池，日志只记
  key 不记 value；
- 缺省（无 `env` 字段）行为与现状完全一致。

### 执行终端命令（job 契约）

命令作为 **job** 在持久会话里执行：`cd` / `export` / `source venv` 会带到下一次调用。
`wait`（默认 30，服务端上限 120）只限制**本次请求**最多等多久；`timeout` 是命令的总时限——
**无默认、无上限**，不传就跑到命令自己结束。全量输出写到容器内的 `log_path`（可在沙盒里
`tail` / `grep`），响应里的 `output` 按 25 KB + 25 KB 的 head/tail 口径截断。

```bash
# 提交；最多等 60s，带着目前为止的状态返回
curl -X POST http://localhost:8088/v1/sandboxes/sb_abc123/proxy/api/terminal/execute \
  -H "Content-Type: application/json" \
  -d '{"command": "pip install openai-whisper", "wait": 60}'
# → {"job_id": "j_01K…", "status": "running", "exit_code": null, "output": "…到目前为止…",
#    "cursor": 4096, "log_path": "/tmp/cr-jobs/j_01K….log", "kill_reason": null, "success": true}

# 长轮询：从 cursor 起取增量输出；job 已结束立即返回
curl -X POST http://localhost:8088/v1/sandboxes/sb_abc123/proxy/api/terminal/wait \
  -H "Content-Type: application/json" \
  -d '{"job_id": "j_01K…", "cursor": 4096, "wait": 60}'
# → {"job_id": "j_01K…", "status": "exited", "exit_code": 0, "output": "…", "cursor": 9120, …}

# 终止（调用方在用户点「停止」时调用）
curl -X POST http://localhost:8088/v1/sandboxes/sb_abc123/proxy/api/terminal/kill \
  -H "Content-Type: application/json" \
  -d '{"job_id": "j_01K…", "signal": "INT"}'     # INT | TERM | KILL
# → {"status": "killed", "exit_code": 130, "kill_reason": "kill:INT", …}
```

`status` 取值 `running | exited | killed`；`kill_reason` 为 `timeout`、`kill:<SIG>` 或 `restart`。
同一时刻只跑一个 job——上一条没结束就再提交返回 **409**，body 带正在跑的 `job_id`。
`POST /api/terminal/restart` 会 kill 当前 job 并复位 cwd / 环境变量。

**旧形态**（过渡期保留）：请求体**不带 `wait`** 即阻塞至命令结束，`timeout` 默认 30s（上限
300s），响应仍含 `success` / `output` / `error` / `system`（外加 job 字段）：

```bash
curl -X POST http://localhost:8088/v1/sandboxes/sb_abc123/proxy/api/terminal/execute \
  -H "Content-Type: application/json" \
  -d '{"command": "ls /workspace", "timeout": 30}'
# → {"success": true, "output": "...", "error": "", "status": "exited", "exit_code": 0, …}
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
| 终端 | `/api/terminal/execute`、`/wait`、`/kill`、`/restart`、`/execute/stream` | 持久会话里 job 化执行 bash、长轮询、终止、SSE 流式输出 |
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

### `.env`：部署接入（本机 / 网络 / 对象存储连接 / 密钥）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SANDBOX_HUB_HOST` | `0.0.0.0` | 监听地址；私有化部署建议收紧为 `127.0.0.1` 或内网 IP |
| `SANDBOX_HUB_PORT` | `8088` | SandboxHub 服务端口 |
| `CONTAINER_LABEL` | `sandboxhub.managed` | 受管容器标签（同机多实例隔离用） |
| `SANDBOX_NETWORK` | `bridge` | 容器所在 Docker 网络 |
| `SANDBOX_HTTP_PROXY` | （空）| 注入 ubuntu 容器的出网代理；宿主代理须写容器可达地址（`host.docker.internal`） |
| `SANDBOX_DNS` | （空）| 容器自定义 DNS（逗号分隔，经 `docker --dns` 注入）；非空时同时注入 `SANDBOX_KEEP_DNS=1`，使 ubuntu 镜像 entrypoint 不覆写 `resolv.conf` |
| `SANDBOX_KEEP_DNS` | `false` | `true`=始终注入 `SANDBOX_KEEP_DNS=1`，容器保留 Docker 注入的 `resolv.conf`（宿主 `daemon.json`/`--dns`）。需重建镜像后生效 |
| `MINIO_ENDPOINT` | （空）| MinIO `host:port`（不含 scheme），写**容器内可达**地址（默认 bridge 网桥宿主为 `172.17.0.1`）；空=不具备挂载能力 |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | （空）| MinIO 凭据（经环境变量内联传给容器内 rclone，不落盘）；须与 createrole 侧指向同一实例 |
| `MINIO_SECURE` | `false` | `true`=https 访问 MinIO |
| `SANDBOX_HUB_API_KEY` | （空）| 可选鉴权：非空时所有请求须带匹配的 `X-API-Key` 头，否则 401；`/v1/health` 豁免。空=不鉴权 |

### `config/system.yaml`：系统调优（进 git，改动走 PR）

| 键 | 默认值 | 说明 |
|----|--------|------|
| `image.ubuntu` / `image.code` | `sandbox-ubuntu:latest` / `sandbox-code:latest` | 两种 profile 的镜像名 |
| `warm_pool.ubuntu` / `warm_pool.code` | `3` / `0`（本仓线上 ubuntu=1） | 预热容器数，0=不预热 |
| `warm_pool.maintain_interval` | `30` | 预热池补齐检查间隔（秒） |
| `sandbox.api_port` | `8000` | 容器内 FastAPI 端口，与镜像 entrypoint 一致 |
| `sandbox.idle_ttl` | `7200` | 已分配沙盒闲置回收阈值（秒），0=关闭 |
| `reconcile.interval` | `60` | 周期对账间隔（秒） |
| `reconcile.orphan_grace_seconds` | `300` | 孤儿容器创建宽限（秒） |
| `proxy.read_timeout` / `proxy.connect_timeout` | `330` / `10` | 代理转发超时（秒）；读超时须大于终端单次请求最长时长（旧契约 `timeout` 上限 300s；job 契约 `wait` 上限 120s） |
| `workspace.mount_enabled` | `true` | 工作区挂载总开关 |
| `workspace.rclone_vfs_cache_mode` | `full` | rclone VFS 缓存模式；`full` 避免写回窗口内 rename-over 报 EIO（issue #9） |
| `workspace.rclone_vfs_cache_max_size` | `2G` | VFS 本地缓存体积上限 |
| `workspace.rclone_vfs_write_back` | `1s` | 文件关闭后回写 MinIO 的延迟 |
| `workspace.rclone_dir_cache_time` | `2s` | 目录列表缓存时长（MinIO→容器可见延迟） |
| `workspace.mount_ready_retries` / `mount_ready_interval` | `20` / `0.5` | 挂载就绪探测轮询次数与间隔（秒） |

系统键声明在 `src/config.py` 的 `SYSTEM_KNOBS`，`tests/unit/test_system_config.py` 对账 yaml 不缺不多。
机器差异（GPU 机 vs 开发机的镜像名 / 预热数）走分支或 PR，不走现场 env。

---

## 离线/私有化部署注意

产品部署到客户内网（断网）机器时的注意事项。沙盒镜像须在断网前于目标机构建好
（或 `docker save`/`docker load` 导入）——构建期需联网，运行期不需要。

**1. 容器内 DNS。** ubuntu 镜像 entrypoint 启动时会把 `/etc/resolv.conf` 覆写为
`8.8.8.8`/`1.1.1.1`，顶掉 `docker --dns` 注入的配置，离线机上容器内域名解析全部超时。
处理：把 `SANDBOX_DNS` 配成客户内网 DNS（或设 `SANDBOX_KEEP_DNS=true` 沿用宿主
Docker 的 DNS 配置），两者都会向容器注入 `SANDBOX_KEEP_DNS=1`，entrypoint 据此跳过
覆写。**须用当前 `entrypoint.sh` 重建镜像后才生效**——旧镜像不识别该变量。
code 镜像从不覆写 `resolv.conf`，只需 `--dns` 即可。

**2. 沙盒内 `pip install` / `npm install`。** 两个镜像都固化了公网国内源：pip 指向
清华 PyPI 镜像（构建期 `pip config set`，见 `images/ubuntu/Dockerfile` 与
`images/code/Dockerfile`），npm/yarn/pnpm 指向 `registry.npmmirror.com`
（`NPM_CONFIG_REGISTRY` 环境变量 + `npm config set`）。断网后沙盒内装包会失败。
SandboxHub **没有**运行时覆盖这些源的配置旋钮。客户内网有私有源时的选项：
- 单次命令（现成可用，agent 可自行执行）：`pip install -i http://<mirror>/simple <pkg>`、
  `npm install --registry=http://<mirror> <pkg>`。跨容器不持久。
- 永久生效：重建镜像，替换两个 Dockerfile 里的 pip `config set` / `NPM_CONFIG_REGISTRY`
  为私有源地址；或构建期把所需包全部预装。

**3. 收紧 API 面。** SandboxHub 等价于任意命令执行入口。共享内网上应配置
`SANDBOX_HUB_API_KEY`（createrole 客户端用同名 env 自动带 `X-API-Key` 头），
与 createrole 同机部署时可把 `SANDBOX_HUB_HOST` 收紧为 `127.0.0.1`。

**4. MinIO 地址。** `MINIO_ENDPOINT` 必须是「容器内可达」的地址：`172.17.0.1:9000`
是默认 `bridge` 网络的宿主网关写法，自建网络/异机 MinIO 须相应调整，且必须与
createrole 侧指向同一 MinIO 实例。

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
3. 在 `src/config.py` 声明 `DOCKER_IMAGE_<TYPE>` / `WARM_POOL_<TYPE>` 字段并加进 `SYSTEM_KNOBS`，在 `config/system.yaml` 的 `image` / `warm_pool` 段填值（对账测试会兜底）
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
