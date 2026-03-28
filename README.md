# SandboxHub

将 Docker 沙盒管理从业务服务中解耦的独立微服务。为每个 (user_id, role_id) 分配运行中的容器，维护预热池（warm pool）消除冷启动，通过 HTTP proxy 透传所有工具调用。

## 架构

```
CreateRoleAPI ──HTTP──→ SandboxHub :8088
                              │
                         proxy forward
                              │
                        Ubuntu 容器 :8000
                        (BashSession PTY)
```

- **acquire**：<100ms 从 warm pool 分配容器（或同 user+role 复用）
- **proxy**：`/v1/sandboxes/{id}/proxy/{path}` 透传给容器 `:8000/{path}`
- **release**：后台清理 workspace，容器归还 pool

## 快速开始

```bash
# 1. 安装依赖
conda activate df
pip install -e .

# 2. 配置
cp .env.example .env
# 编辑 .env：DOCKER_IMAGE_UBUNTU、WARM_POOL_UBUNTU 等

# 3. 启动
uvicorn src.main:app --host 0.0.0.0 --port 8088 --reload

# 4. 健康检查
curl http://localhost:8088/v1/health
# → {"ok": true, "warm_pool": {"ubuntu": {"available": 3}}}
```

## 接口

### acquire
```bash
curl -X POST http://localhost:8088/v1/sandboxes/acquire \
  -H "Content-Type: application/json" \
  -d '{"user_id": "u1", "role_id": "r1", "sandbox_type": "ubuntu"}'
# → {"sandbox_id": "sb_abc123", "status": "ready"}
```

### 工具调用（proxy）
```bash
curl -X POST http://localhost:8088/v1/sandboxes/sb_abc123/proxy/api/terminal/execute \
  -H "Content-Type: application/json" \
  -d '{"command": "ls /workspace", "timeout": 30}'
# → {"success": true, "output": "...", "error": null}
```

### release
```bash
curl -X POST http://localhost:8088/v1/sandboxes/sb_abc123/release
# → {"ok": true}
```

### status
```bash
curl http://localhost:8088/v1/sandboxes/sb_abc123/status
curl http://localhost:8088/v1/sandboxes
```

## 配置项

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SANDBOX_HUB_PORT` | 8088 | 服务端口 |
| `DOCKER_IMAGE_UBUNTU` | sandbox-ubuntu:latest | Ubuntu 沙盒镜像名 |
| `WARM_POOL_UBUNTU` | 3 | Ubuntu 预热容器数量 |
| `SANDBOX_NETWORK` | bridge | Docker 网络模式 |
| `SANDBOX_API_PORT` | 8000 | 容器内 API 端口 |
| `POOL_MAINTAIN_INTERVAL` | 30 | 池补充检查间隔（秒） |
| `SANDBOX_HTTP_PROXY` | （空）| 注入容器的 HTTP 代理 |

## 扩展新沙盒类型

1. 在 `config.py` 的 `image_for_type()` 和 `pool_size_for_type()` 中加入新类型
2. 在 `warm_pool.py` 的 `maintain_loop()` 的类型列表中加入新类型
3. 在 `.env` 中加入 `WARM_POOL_<TYPE>=N`
4. 无需修改 Registry / Router / Proxy

## 与 CreateRoleAPI 集成

CreateRoleAPI 的 `SandboxHubClient` 封装了 acquire/release/proxy 调用，配置 `SANDBOX_HUB_URL=http://localhost:8088` 即可。
详见 `server/sandbox/sandbox_client.py`。
