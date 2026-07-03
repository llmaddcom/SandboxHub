# /data/zh/SandboxHub/src/routers/proxy.py
"""
Proxy 路由

职责：将 /v1/sandboxes/{sandbox_id}/proxy/{path} 转发给对应容器。
依赖 registry 查找容器 IP，依赖 forwarder 执行转发。
转发即刷新沙盒活跃时间（供闲置回收判定）；转发失败(502)时后台触发即时体检，
容器已死则驱逐销毁，让调用方的下一次 acquire 立即重建而不必等对账周期。
"""
import asyncio

from fastapi import APIRouter, HTTPException, Request, Response

from src.proxy.forwarder import forward

router = APIRouter()

# registry / reconciler 在 app 启动后注入
_registry = None
_reconciler = None


def set_dependencies(registry, reconciler) -> None:
    global _registry, _reconciler
    _registry = registry
    _reconciler = reconciler


@router.api_route(
    "/{sandbox_id}/proxy/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def proxy_request(sandbox_id: str, path: str, request: Request) -> Response:
    """透传请求到沙盒容器，原样返回响应。"""
    if _registry is None:
        raise HTTPException(status_code=503, detail="registry not initialized")

    record = await _registry.get(sandbox_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"sandbox {sandbox_id!r} not found")
    if record.status != "ready":
        raise HTTPException(status_code=409, detail=f"sandbox {sandbox_id!r} is not ready (status={record.status})")

    _registry.touch(sandbox_id)
    response = await forward(record.container_info.container_ip, path, request)
    if response.status_code == 502 and _reconciler is not None:
        # 转发失败可能是容器已死：后台体检（内部有 TCP 探测兜底，容器仍可达则不动），
        # 死则驱逐销毁，让下一次 acquire 立即重建。
        asyncio.create_task(_reconciler.evict_if_dead(sandbox_id))
    return response
