# /data/zh/SandboxHub/tests/unit/test_forwarder.py
"""
forwarder 单元测试

验证模块级连接池的生命周期行为（close_client / close_all_clients）。
"""
import pytest
from unittest.mock import AsyncMock, patch

import src.proxy.forwarder as forwarder_module
from src.proxy.forwarder import close_client, close_all_clients


@pytest.fixture(autouse=True)
def reset_client_pool():
    """每个测试前后清空连接池，防止测试间状态污染。"""
    forwarder_module._client_pool.clear()
    yield
    forwarder_module._client_pool.clear()


@pytest.mark.asyncio
async def test_close_client_removes_from_pool():
    mock_client = AsyncMock()
    forwarder_module._client_pool["172.17.0.5"] = mock_client
    await close_client("172.17.0.5")
    mock_client.aclose.assert_awaited_once()
    assert "172.17.0.5" not in forwarder_module._client_pool


@pytest.mark.asyncio
async def test_close_client_noop_for_unknown_ip():
    # should not raise
    await close_client("1.2.3.4")


@pytest.mark.asyncio
async def test_close_all_clients_closes_all():
    mock1 = AsyncMock()
    mock2 = AsyncMock()
    forwarder_module._client_pool["172.17.0.5"] = mock1
    forwarder_module._client_pool["172.17.0.6"] = mock2
    await close_all_clients()
    mock1.aclose.assert_awaited_once()
    mock2.aclose.assert_awaited_once()
    assert len(forwarder_module._client_pool) == 0


# ── 结构化错误响应（issue #4）─────────────────────────────────────────────────

import json

import httpx
from unittest.mock import MagicMock


def _fake_request() -> MagicMock:
    req = MagicMock()
    req.body = AsyncMock(return_value=b"")
    req.headers = {}
    req.method = "POST"
    req.query_params = {}
    return req


async def _forward_with_error(exc: Exception):
    from src.proxy.forwarder import forward

    client = AsyncMock()
    client.request = AsyncMock(side_effect=exc)
    forwarder_module._client_pool["172.17.0.9"] = client
    return await forward("172.17.0.9", "api/terminal/execute", _fake_request())


@pytest.mark.asyncio
async def test_connect_error_returns_structured_502():
    resp = await _forward_with_error(httpx.ConnectError("[Errno 2] No such file or directory"))
    assert resp.status_code == 502
    body = json.loads(resp.body)
    assert body["reason"] == "upstream_unreachable"
    assert body["error"].startswith("proxy error:")
    assert "acquire" in body["detail"]


@pytest.mark.asyncio
async def test_read_timeout_returns_structured_504():
    resp = await _forward_with_error(httpx.ReadTimeout("timed out"))
    assert resp.status_code == 504
    body = json.loads(resp.body)
    assert body["reason"] == "upstream_timeout"


@pytest.mark.asyncio
async def test_unknown_error_returns_structured_502():
    # 错误信息含引号等特殊字符时，body 仍须是合法 JSON（json.dumps 转义）
    resp = await _forward_with_error(RuntimeError('boom "quoted" \\ path'))
    assert resp.status_code == 502
    body = json.loads(resp.body)
    assert body["reason"] == "proxy_error"
    assert '"quoted"' in body["error"]
