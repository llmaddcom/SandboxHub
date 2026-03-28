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
