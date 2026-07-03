"""
SandboxHub FastAPI 应用入口

lifespan：
  startup  → 初始化 ContainerManager / Registry / WarmPool / Reconciler
           → 启动恢复：清理已停止/孤儿容器，健康的遗留容器清理复位后收养回 pool
           → 预热 pool 到目标大小
           → 启动 pool 维护 + 沙盒对账两个后台任务
  shutdown → 取消后台任务，清理全部容器，关闭所有 httpx 连接池
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, APIRouter
from loguru import logger

from .config import settings
from .manager.container_manager import ContainerManager
from .manager.reconciler import SandboxReconciler
from .manager.registry import SandboxRegistry
from .manager.warm_pool import WarmPool
from .routers import proxy as proxy_router
from .routers import sandboxes as sandboxes_router
from .proxy.forwarder import close_all_clients


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ──────────────────────────────────────────────────────────────
    container_manager = ContainerManager()
    registry = SandboxRegistry()
    warm_pool = WarmPool(container_manager)
    reconciler = SandboxReconciler(registry, warm_pool, container_manager)

    # 启动恢复：处置 Docker 遗留的受管容器（已停止/挂载孤儿销毁，健康 warm 收养回池）
    await reconciler.startup()

    # 注入依赖
    sandboxes_router.set_dependencies(registry, warm_pool, container_manager, reconciler)
    proxy_router.set_dependencies(registry, reconciler)

    # 预热 pool（后台，不阻塞启动）
    for sandbox_type in settings.sandbox_types:
        target = settings.pool_size_for_type(sandbox_type)
        if target > 0:
            logger.info(f"预热 pool | type={sandbox_type} | target={target}")
            asyncio.create_task(warm_pool.ensure_pool(sandbox_type))

    # 启动后台任务：pool 维护 + 沙盒对账
    maintain_task = asyncio.create_task(warm_pool.maintain_loop())
    reconcile_task = asyncio.create_task(reconciler.loop())
    logger.info("SandboxHub 启动完成")

    yield

    # ── shutdown ─────────────────────────────────────────────────────────────
    for task in (maintain_task, reconcile_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # 并发清理所有容器（warm pool + 已分配），确保无孤儿容器
    allocated_infos = await registry.drain()
    await asyncio.gather(
        warm_pool.drain(),
        *[container_manager.remove_container(ci.container_id) for ci in allocated_infos],
        return_exceptions=True,
    )

    await close_all_clients()
    logger.info("SandboxHub 已关闭，所有容器已清理")


app = FastAPI(title="SandboxHub", version="0.1.0", lifespan=lifespan)

app.include_router(sandboxes_router.router)

# proxy router 挂在 /v1/sandboxes 下
_proxy_api = APIRouter(prefix="/v1/sandboxes")
_proxy_api.include_router(proxy_router.router)
app.include_router(_proxy_api)


@app.get("/v1/health")
async def health():
    """健康检查，返回服务状态、warm pool 状态和已分配沙盒数。"""
    pool_status = sandboxes_router._warm_pool.status() if sandboxes_router._warm_pool else {}
    ready = len(sandboxes_router._registry.list_ready()) if sandboxes_router._registry else 0
    return {"ok": True, "warm_pool": pool_status, "sandboxes_ready": ready}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.main:app", host="0.0.0.0", port=8088, reload=False)

