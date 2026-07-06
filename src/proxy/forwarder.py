# /data/zh/SandboxHub/src/proxy/forwarder.py
"""
HTTP 透传转发器

职责：将 /v1/sandboxes/{id}/proxy/{path} 的请求原样转发给容器的 :8000/{path}。
使用模块级 AsyncClient 连接池，避免每次请求新建 TCP 连接。
"""
from __future__ import annotations

import json

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
            # 读超时须覆盖终端命令的最大执行预算（300s）+ 回传余量，否则长命令被
            # 代理层先掐断成 502；建连超时独立收紧，让「容器已死」快速失败。
            timeout=httpx.Timeout(
                settings.PROXY_READ_TIMEOUT, connect=settings.PROXY_CONNECT_TIMEOUT
            ),
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
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        # 容器不可达（已死/被回收/网络消失）。区分于「命令失败」，给调用方可执行指引；
        # proxy 路由层看到 502 会触发即时体检，死容器被驱逐，下一次 acquire 自动重建。
        logger.warning(f"proxy 上游不可达 | ip={container_ip} | path={path} | err={e}")
        return _error_response(
            502,
            error=f"proxy error: {e}",
            reason="upstream_unreachable",
            detail=(
                "沙盒容器不可达（可能已退出或被回收），已触发体检自愈。"
                "请重新 acquire 获取沙盒后重试本次调用。"
            ),
        )
    except httpx.TimeoutException as e:
        # 容器可达但响应超时（区别于容器已死，不应触发驱逐）。
        logger.warning(f"proxy 上游超时 | ip={container_ip} | path={path} | err={e}")
        return _error_response(
            504,
            error=f"proxy error: {e}",
            reason="upstream_timeout",
            detail="沙盒容器响应超时（容器仍在运行），请稍后重试或缩短命令执行时间。",
        )
    except Exception as e:
        logger.warning(f"proxy forward 失败 | ip={container_ip} | path={path} | err={e}")
        return _error_response(
            502,
            error=f"proxy error: {e}",
            reason="proxy_error",
            detail="代理转发失败，已触发沙盒体检。若持续失败请重新 acquire。",
        )


def _error_response(status_code: int, **payload: str) -> Response:
    """构造结构化 JSON 错误响应（json.dumps 保证转义正确，错误信息可含任意字符）。"""
    return Response(
        content=json.dumps(payload, ensure_ascii=False),
        status_code=status_code,
        media_type="application/json",
    )
