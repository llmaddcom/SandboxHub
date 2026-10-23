"""
SandboxHub FastAPI 应用入口

lifespan：
  startup  → 初始化 ContainerManager / Registry / WarmPool / Reconciler
           → 启动恢复：清理已停止/孤儿容器，健康的遗留容器清理复位后收养回 pool
           → 预热 pool 到目标大小
           → 启动 pool 维护 + 沙盒对账两个后台任务
  shutdown → 取消后台任务，清理全部 v1 容器，关闭所有 httpx 连接池

v2（按工作区分配的执行实例，ADR 0025）：启动时准备 v2 网络 / 网关 / cr-fs / netguard 并收养
遗留实例，后台跑首次隔离自检；关闭时 v2 实例、cr-fs、v2 网关一律保留（重启后从标签恢复）。
"""
from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger

from .config import settings
from .manager.container_manager import ContainerManager
from .manager.fs_sidecar import FsSidecar
from .manager.reconciler import SandboxReconciler
from .manager.registry import SandboxRegistry
from .manager.wa_instances import WorkAreaService
from .manager.warm_pool import WarmPool
from .routers import proxy as proxy_router
from .routers import sandboxes as sandboxes_router
from .routers import v2 as v2_router
from .proxy.forwarder import close_all_clients


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ──────────────────────────────────────────────────────────────
    container_manager = ContainerManager()
    registry = SandboxRegistry()
    warm_pool = WarmPool(container_manager)
    if settings.files_cutover_active:
        sandboxes_router.set_draining(True)
        warm_pool.paused = True
    reconciler = SandboxReconciler(registry, warm_pool, container_manager)

    # 网络策略地基：两张受管网络 + cr-host 网关容器（幂等；SANDBOX_NETWORK 为内置网络时跳过）
    await container_manager.ensure_networks()
    await container_manager.ensure_gateway()

    # 启动恢复：处置 Docker 遗留的受管容器（已停止/挂载孤儿销毁，健康 warm 收养回池）
    await reconciler.startup()

    # v2 地基与收养（失败只让 v2 自检不通过，不影响 v1）
    work_areas = WorkAreaService(container_manager, FsSidecar(container_manager.docker))
    await work_areas.startup()
    reconciler.attach_work_areas(work_areas)
    selftest_task = (
        asyncio.create_task(work_areas.run_selftest()) if work_areas.started else None
    )

    # 注入依赖
    sandboxes_router.set_dependencies(registry, warm_pool, container_manager, reconciler)
    proxy_router.set_dependencies(registry, reconciler)
    v2_router.set_dependencies(work_areas, registry, warm_pool, reconciler)

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
    for task in (maintain_task, reconcile_task, selftest_task):
        if task is None:
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # 并发清理所有 v1 容器（warm pool + 已分配），确保无孤儿容器。v2 实例不在 registry 里，保留。
    allocated_infos = await registry.drain()
    await asyncio.gather(
        warm_pool.drain(),
        *[container_manager.remove_container(ci.container_id) for ci in allocated_infos],
        return_exceptions=True,
    )

    await container_manager.remove_gateway()
    await close_all_clients()
    logger.info("SandboxHub 已关闭，所有容器已清理")


app = FastAPI(title="SandboxHub", version="0.1.0", lifespan=lifespan)

# ── 可选 API 鉴权 ────────────────────────────────────────────────────────────────
# SANDBOX_HUB_API_KEY 非空时，所有请求须带匹配的 X-API-Key 头，否则 401。
# 健康检查豁免（供 LB/监控探活）。空 = 完全不鉴权（默认，行为不变）。
_AUTH_EXEMPT_PATHS = frozenset({"/v1/health"})


@app.middleware("http")
async def _api_key_auth(request: Request, call_next):
    expected = settings.SANDBOX_HUB_API_KEY
    if expected and request.url.path not in _AUTH_EXEMPT_PATHS:
        provided = request.headers.get("X-API-Key", "")
        if not secrets.compare_digest(provided, expected):
            return JSONResponse(
                status_code=401, content={"detail": "invalid or missing X-API-Key"}
            )
    return await call_next(request)

app.include_router(sandboxes_router.router)
app.include_router(v2_router.router)

# proxy router 挂在 /v1/sandboxes 下
_proxy_api = APIRouter(prefix="/v1/sandboxes")
_proxy_api.include_router(proxy_router.router)
app.include_router(_proxy_api)


@app.get("/v1/health")
async def health():
    """健康检查，返回服务状态、warm pool 状态和已分配沙盒数。"""
    pool_status = sandboxes_router._warm_pool.status() if sandboxes_router._warm_pool else {}
    ready = len(sandboxes_router._registry.list_ready()) if sandboxes_router._registry else 0
    service = v2_router._service
    return {
        "ok": True,
        "warm_pool": pool_status,
        "sandboxes_ready": ready,
        # 网络策略能力：binary = 支持 policy.network.default 二值热切换；none = 不可用
        "network_policy": "binary" if settings.network_policy_supported else "none",
        "draining": sandboxes_router.is_draining(),
        # v2 概况（细节见 GET /v2/capabilities）
        "work_area": {
            "enabled": settings.WORK_AREA_ENABLED,
            "ok": bool(service) and not service.selftest.result.failing(),
            "instances": len(service.registry.list_all()) if service else 0,
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "src.main:app",
        host=settings.SANDBOX_HUB_HOST,
        port=settings.SANDBOX_HUB_PORT,
        reload=False,
    )
