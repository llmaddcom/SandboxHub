"""
可选 API 鉴权中间件测试（SANDBOX_HUB_API_KEY）

不触发 lifespan（ASGITransport 不跑 startup），只验证中间件行为：
- key 为空（默认）：完全不鉴权，行为不变。
- key 非空：缺头/错头 401；对头放行；/v1/health 豁免。
中间件先于路由执行，用不存在的路径断言「放行=404、拦截=401」，避免依赖未装配的路由依赖。
"""
import pytest
from httpx import AsyncClient, ASGITransport

from src.config import settings
from src.main import app


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_no_key_configured_no_auth(client, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_HUB_API_KEY", "")
    async with client:
        r = await client.get("/v1/health")
        assert r.status_code == 200
        # 无鉴权时未知路径直接进路由 → 404 而非 401
        r = await client.get("/nonexistent")
        assert r.status_code == 404


async def test_key_configured_rejects_missing_or_wrong_header(client, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_HUB_API_KEY", "secret-key")
    async with client:
        r = await client.get("/nonexistent")
        assert r.status_code == 401
        r = await client.get("/nonexistent", headers={"X-API-Key": "wrong"})
        assert r.status_code == 401


async def test_key_configured_accepts_matching_header(client, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_HUB_API_KEY", "secret-key")
    async with client:
        # 对头放行，进入路由 → 404（路径不存在），说明未被鉴权拦截
        r = await client.get("/nonexistent", headers={"X-API-Key": "secret-key"})
        assert r.status_code == 404


async def test_health_exempt_from_auth(client, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_HUB_API_KEY", "secret-key")
    async with client:
        r = await client.get("/v1/health")
        assert r.status_code == 200
