# /data/zh/SandboxHub/src/routers/sandboxes.py
"""
沙盒生命周期路由

职责：暴露 acquire/release/status/list/ping 接口。
acquire 优先复用已有 sandbox，其次从 warm pool 取，最后冷启动兜底。
release 触发后台清理，立即返回 ok。
ping 提供浅检查（registry 状态）和深检查（TCP 可达性）两种健康检查模式。
"""
from __future__ import annotations

import asyncio
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel

router = APIRouter(prefix="/v1/sandboxes")

# 依赖注入（app 启动后通过 set_* 注入）
_registry = None
_warm_pool = None
_container_manager = None


def set_dependencies(registry, warm_pool, container_manager) -> None:
    global _registry, _warm_pool, _container_manager
    _registry = registry
    _warm_pool = warm_pool
    _container_manager = container_manager


# ── 请求/响应模型 ──────────────────────────────────────────────────────────────

class AcquireRequest(BaseModel):
    user_id: str
    role_id: str
    sandbox_type: Literal["ubuntu"] = "ubuntu"


class AcquireResponse(BaseModel):
    sandbox_id: str
    status: str


class StatusResponse(BaseModel):
    sandbox_id: str
    user_id: str
    role_id: str
    sandbox_type: str
    status: str
    container_ip: str
    acquired_at: str


class PingResponse(BaseModel):
    ok: bool
    status: str
    container_ip: str
    reachable: Optional[bool] = None


# ── 接口 ──────────────────────────────────────────────────────────────────────

@router.post("/acquire", response_model=AcquireResponse)
async def acquire_sandbox(req: AcquireRequest) -> AcquireResponse:
    """
    获取沙盒。

    1. 复用已有 ready sandbox（同 user+role）
    2. 从 warm pool 取一个
    3. 兜底：冷启动新容器
    后台异步补充 pool，不阻塞返回。
    """
    # 1. 复用
    existing = await _registry.find_active(req.user_id, req.role_id)
    if existing:
        logger.debug(f"复用 sandbox | id={existing.sandbox_id} | user={req.user_id}")
        return AcquireResponse(sandbox_id=existing.sandbox_id, status="ready")

    # 2. warm pool
    container = await _warm_pool.acquire(req.sandbox_type)

    # 3. 兜底冷启动
    if container is None:
        logger.warning(f"warm pool 为空，冷启动 | type={req.sandbox_type}")
        try:
            container = await _container_manager.run_container(req.sandbox_type)
        except Exception as e:
            logger.error(f"冷启动失败 | type={req.sandbox_type} | err={e}")
            raise HTTPException(status_code=503, detail=f"cold start failed: {e}")

    record = await _registry.register(container, req.user_id, req.role_id)

    # 后台补充 pool
    asyncio.create_task(_warm_pool.ensure_pool(req.sandbox_type))

    logger.info(f"sandbox 已分配 | id={record.sandbox_id} | ip={container.container_ip}")
    return AcquireResponse(sandbox_id=record.sandbox_id, status="ready")


@router.post("/{sandbox_id}/release")
async def release_sandbox(sandbox_id: str) -> dict:
    """
    释放沙盒。后台清理容器 workspace，立即返回 ok。
    """
    container_info = await _registry.mark_released(sandbox_id)
    if container_info is None:
        raise HTTPException(status_code=404, detail=f"sandbox {sandbox_id!r} not found")

    # 后台清理并归还 pool
    asyncio.create_task(_warm_pool.release(container_info))
    logger.info(f"sandbox 已释放 | id={sandbox_id}")
    return {"ok": True}


@router.get("/{sandbox_id}/status", response_model=StatusResponse)
async def get_status(sandbox_id: str) -> StatusResponse:
    record = await _registry.get(sandbox_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"sandbox {sandbox_id!r} not found")
    return StatusResponse(
        sandbox_id=record.sandbox_id,
        user_id=record.user_id,
        role_id=record.role_id,
        sandbox_type=record.container_info.sandbox_type,
        status=record.status,
        container_ip=record.container_info.container_ip,
        acquired_at=record.acquired_at.isoformat(),
    )


@router.get("/{sandbox_id}/ping", response_model=PingResponse, response_model_exclude_none=True)
async def ping_sandbox(sandbox_id: str, deep: bool = False) -> PingResponse:
    """
    检查沙盒健康状态。

    deep=false（默认）：浅检查，仅查询 registry 是否存在且 status=ready。
    deep=true：在浅检查基础上，TCP 探测容器 API 端口是否可达。
    """
    record = await _registry.get(sandbox_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"sandbox {sandbox_id!r} not found")

    ip = record.container_info.container_ip
    if not deep:
        return PingResponse(ok=record.status == "ready", status=record.status, container_ip=ip)

    reachable = await _container_manager.is_healthy(ip)
    return PingResponse(ok=reachable, status=record.status, container_ip=ip, reachable=reachable)


@router.get("")
async def list_sandboxes() -> dict:
    records = _registry.list_all()
    return {
        "sandboxes": [
            {
                "sandbox_id": r.sandbox_id,
                "user_id": r.user_id,
                "role_id": r.role_id,
                "status": r.status,
                "sandbox_type": r.container_info.sandbox_type,
            }
            for r in records
        ]
    }
