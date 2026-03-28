# /data/zh/SandboxHub/src/proxy/forwarder.py
"""
HTTP 透传转发器

职责：将 /v1/sandboxes/{id}/proxy/{path} 的请求原样转发给容器的 :8000/{path}。
使用模块级 AsyncClient 连接池，避免每次请求新建 TCP 连接。
"""
from __future__ import annotations

import httpx
from fastapi import Request, Response
from loguru import logger

from src.config import settings

# 模块级连接池，按容器 IP 缓存 client
_client_pool: dict[str, httpx.AsyncClient] = {}

# hop-by-hop 请求头，不透传给容器
_SKIP_REQ_HEADERS = {"host", "content-length", "transfer-encoding"}

# httpx 会自动解压响应体；移除此头以匹配已解码的内容
_SKIP_RESP_HEADERS = {"transfer-encoding", "content-encoding"}


def _get_client(container_ip: str) -> httpx.AsyncClient:
    if container_ip not in _client_pool:
        _client_pool[container_ip] = httpx.AsyncClient(
            base_url=f"http://{container_ip}:{settings.SANDBOX_API_PORT}",
            timeout=httpx.Timeout(150.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
    return _client_pool[container_ip]


async def close_client(container_ip: str) -> None:
    """关闭并移除指定 IP 的连接池 client。容器销毁时调用，防止连接泄漏和跨沙盒错误路由。"""
    client = _client_pool.pop(container_ip, None)
    if client:
        await client.aclose()


async def close_all_clients() -> None:
    """关闭所有缓存的连接池 client，通常在应用关闭时调用。"""
    for ip in list(_client_pool.keys()):
        await close_client(ip)


async def forward(container_ip: str, path: str, request: Request) -> Response:
    """
    透传 HTTP 请求到容器 API。

    - 保留原始 method / body / query string
    - 过滤 hop-by-hop 请求头（host, content-length）
    - 原样返回容器响应（status / body / content-type）
    """
    body = await request.body()
    req_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _SKIP_REQ_HEADERS
    }

    try:
        client = _get_client(container_ip)
        resp = await client.request(
            method=request.method,
            url=f"/{path}",
            content=body,
            headers=req_headers,
            params=dict(request.query_params),
        )
        resp_headers = {
            k: v for k, v in resp.headers.items()
            if k.lower() not in _SKIP_RESP_HEADERS
        }
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    except Exception as e:
        logger.warning(f"proxy forward 失败 | ip={container_ip} | path={path} | err={e}")
        return Response(
            content=f'{{"error": "proxy error: {e}"}}',
            status_code=502,
            media_type="application/json",
        )
