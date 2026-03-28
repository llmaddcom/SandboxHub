"""
SandboxHub FastAPI 应用入口

lifespan：
  startup  → 初始化 ContainerManager / Registry / WarmPool
           → 从 Docker 恢复已运行的受管容器
           → 预热 pool 到目标大小
           → 启动 pool 维护后台任务
  shutdown → 取消维护任务，关闭所有 httpx 连接池
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, APIRouter
from loguru import logger

from src.config import settings
from src.manager.container_manager import ContainerManager
from src.manager.registry import SandboxRegistry
from src.manager.warm_pool import WarmPool
from src.routers import proxy as proxy_router
from src.routers import sandboxes as sandboxes_router
from src.proxy.forwarder import close_all_clients


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ──────────────────────────────────────────────────────────────
    container_manager = ContainerManager()
    registry = SandboxRegistry()
    warm_pool = WarmPool(container_manager)

    # 恢复已运行的受管容器到 warm pool（仅 warm/未分配状态，不重建 Registry）
    recovered = container_manager.recover_running_containers()
    for info in recovered:
        await warm_pool.restore(info)
    if recovered:
        logger.info(f"恢复 {len(recovered)} 个运行中容器到 warm pool")

    # 注入依赖
    sandboxes_router.set_dependencies(registry, warm_pool, container_manager)
    proxy_router.set_registry(registry)

    # 预热 pool（后台，不阻塞启动）
    for sandbox_type in ("ubuntu",):
        target = settings.pool_size_for_type(sandbox_type)
        if target > 0:
            logger.info(f"预热 pool | type={sandbox_type} | target={target}")
            asyncio.create_task(warm_pool.ensure_pool(sandbox_type))

    # 启动维护后台任务
    maintain_task = asyncio.create_task(warm_pool.maintain_loop())
    logger.info("SandboxHub 启动完成")

    yield

    # ── shutdown ─────────────────────────────────────────────────────────────
    maintain_task.cancel()
    try:
        await maintain_task
    except asyncio.CancelledError:
        pass

    # 关闭所有缓存的 httpx 连接池
    await close_all_clients()

    logger.info("SandboxHub 已关闭")


app = FastAPI(title="SandboxHub", version="0.1.0", lifespan=lifespan)

app.include_router(sandboxes_router.router)

# proxy router 挂在 /v1/sandboxes 下
_proxy_api = APIRouter(prefix="/v1/sandboxes")
_proxy_api.include_router(proxy_router.router)
app.include_router(_proxy_api)


@app.get("/v1/health")
async def health():
    """健康检查，返回服务状态和 warm pool 状态。"""
    pool_status = sandboxes_router._warm_pool.status() if sandboxes_router._warm_pool else {}
    return {"ok": True, "warm_pool": pool_status}
