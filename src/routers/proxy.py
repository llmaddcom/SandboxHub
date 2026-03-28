# /data/zh/SandboxHub/src/routers/proxy.py
"""
Proxy 路由

职责：将 /v1/sandboxes/{sandbox_id}/proxy/{path} 转发给对应容器。
依赖 registry 查找容器 IP，依赖 forwarder 执行转发。
"""
from fastapi import APIRouter, HTTPException, Request, Response

from src.proxy.forwarder import forward

router = APIRouter()

# registry 在 app 启动后注入
_registry = None


def set_registry(registry) -> None:
    global _registry
    _registry = registry


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

    return await forward(record.container_info.container_ip, path, request)
