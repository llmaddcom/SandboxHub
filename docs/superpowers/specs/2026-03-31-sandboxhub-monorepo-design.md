# SandboxHub Monorepo 设计文档

**日期**：2026-03-31
**范围**：SandboxHub + Ubuntu 沙盒镜像合并为单一项目，含四项改进

---

## 背景

当前有两个独立项目：

- `/data/zh/Ubuntu` — Ubuntu 22.04 沙盒镜像（Dockerfile + 容器启动脚本 + 容器内 FastAPI/MCP 服务）
- `/data/zh/SandboxHub` — 调度层（warm pool、容器生命周期管理、反向代理）

两者紧密耦合但分开维护，镜像构建与调度层版本容易失步。本次将其合并为一个 monorepo，同时解决启动方式、镜像构建网络问题和终端流式输出。

---

## 任务一：Monorepo 目录结构

### 目标结构

```
SandboxHub/
├── main.py                          # 根入口，python main.py 启动调度层
├── pyproject.toml                   # SandboxHub 依赖（不变）
├── .env
├── src/                             # SandboxHub 调度层（不动）
│   ├── __init__.py
│   ├── config.py
│   ├── main.py                      # FastAPI app（相对 import，不直接运行）
│   ├── manager/
│   ├── proxy/
│   └── routers/
├── images/
│   └── ubuntu/
│       ├── Dockerfile               # 从 Ubuntu/ 移入，更新 COPY 路径
│       ├── scripts/                 # 原 Ubuntu/image/（容器启动脚本）
│       │   ├── entrypoint.sh
│       │   ├── start_all.sh
│       │   └── *.sh
│       └── app/                     # 原 Ubuntu/src/（容器内 FastAPI + MCP）
│           ├── main.py
│           ├── mcp_server.py
│           ├── routers/
│           └── tools/
├── docs/
└── tests/
```

### 迁移步骤

1. 在 SandboxHub 内创建 `images/ubuntu/` 目录
2. 将 `Ubuntu/image/` → `images/ubuntu/scripts/`
3. 将 `Ubuntu/src/` → `images/ubuntu/app/`
4. 将 `Ubuntu/Dockerfile` → `images/ubuntu/Dockerfile`
5. 更新 Dockerfile 中的 COPY 路径：
   - `COPY --chown=... scripts/ $HOME/`（原 `image/`）
   - `COPY --chown=... app/ $HOME/computer_use_demo/`（原 `src/`）
6. 构建命令变为：`docker build -t sandbox-ubuntu images/ubuntu/`

### 扩展性

后续新增镜像类型只需在 `images/` 下添加新目录，如 `images/debian/`、`images/windows/`。`src/config.py` 中的 `image_for_type()` 映射同步扩展。

---

## 任务二：根目录 `main.py` 入口

### 问题

`src/main.py` 使用相对 import（`from .config import settings`），无法直接 `python src/main.py` 运行。

### 方案

在项目根目录新建 `main.py`，注入 `sys.path` 后以字符串形式加载 `src.main:app`：

```python
# main.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import uvicorn
from src.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=settings.SANDBOX_HUB_PORT,
        reload=False,
    )
```

- `sys.path` 注入让 Python 能找到 `src` package
- uvicorn 以字符串形式导入 `src.main:app`，相对 import 在 package 上下文中正常工作
- `src/main.py` 的 `__main__` 块可同步更新为相同逻辑，但主入口统一走根目录

---

## 任务三：Dockerfile 优化

### 3.1 COPY 路径更新

```dockerfile
COPY --chown=$USERNAME:$USERNAME scripts/ $HOME/
COPY --chown=$USERNAME:$USERNAME app/ $HOME/computer_use_demo/
```

### 3.2 国内镜像源替换 GitHub 克隆

build 阶段直接访问 GitHub 在国内网络下不稳定，改用 Gitee 镜像：

```dockerfile
# noVNC（原 github.com/novnc）
RUN git clone --branch v1.5.0 https://gitee.com/mirrors/noVNC.git /opt/noVNC && \
    git clone --branch v0.12.0 https://gitee.com/mirrors/websockify /opt/noVNC/utils/websockify && \
    ln -s /opt/noVNC/vnc.html /opt/noVNC/index.html

# pyenv（原 github.com/pyenv/pyenv）
RUN git clone https://gitee.com/mirrors/pyenv.git ~/.pyenv && \
    ...
```

### 3.3 多架构保持不变

Dockerfile 中 amd64/arm64 分支逻辑保留。迁移到 x86 机器后，`dpkg --print-architecture` 自动返回 `amd64`，走 Google Chrome + x64 Node 路径，无需手动修改。

运行时 DNS 修复（`entrypoint.sh` 第 0 步）保持不变。

---

## 任务四：终端流式输出

### 4.1 底层：降低轮询延迟

`bash.py` 中 `BashSession` 的轮询间隔从 200ms 降至 20ms：

```python
# 原
_output_delay = 0.2
# 改
_output_delay = 0.02
```

### 4.2 底层：新增 `execute_stream()` 异步生成器

`BashSession` 新增方法，写入命令后持续读取输出 buffer，每有内容即 yield，直到检测到哨兵：

```python
async def execute_stream(self, command: str, timeout: float = DEFAULT_TIMEOUT):
    """逐块 yield 命令输出，直到哨兵出现。"""
    # 写入命令（同现有逻辑）
    # 循环读取，yield 非空 chunk
    # 检测到哨兵时 yield done 并返回
```

`BashTool` 对应新增 `execute_stream()` 包装方法。

### 4.3 接口层：SSE 端点

`terminal.py` 新增：

```python
@router.post("/execute/stream")
async def execute_stream(request: ExecuteRequest):
    async def event_gen():
        async for event in bash_tool.execute_stream(request.command, request.timeout):
            yield f"data: {json.dumps(event)}\n\n"
    return StreamingResponse(event_gen(), media_type="text/event-stream")
```

SSE 事件格式：

| 类型 | 字段 | 说明 |
|------|------|------|
| `stdout` | `{"type": "stdout", "chunk": "..."}` | 标准输出块 |
| `stderr` | `{"type": "stderr", "chunk": "..."}` | 错误输出块 |
| `done` | `{"type": "done"}` | 命令执行完毕 |

现有 `POST /api/terminal/execute` 接口不变，受益于 4.1 的延迟降低。

---

## 不在本次范围内

- Ubuntu 项目的 CLAUDE.md 中 P0/P1 已知问题（并发保护、私有 API 等）
- 新增其他镜像类型
- SandboxHub 速率限制、结构化日志等 P2 项
