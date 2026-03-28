# /data/zh/SandboxHub/tests/integration/test_sandboxes_api.py
"""
沙盒 API 集成测试（通过 FastAPI TestClient，mock 外部依赖）

不连接真实 Docker，通过 mock registry/warm_pool/container_manager 验证路由逻辑。
"""
import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock
from httpx import AsyncClient, ASGITransport
from fastapi import FastAPI

from src.routers import sandboxes as sandboxes_router
from src.routers import proxy as proxy_router
from src.models import ContainerInfo, SandboxRecord
from datetime import datetime, timezone


def make_container(ip: str = "172.17.0.5") -> ContainerInfo:
    return ContainerInfo(
        container_id="cid_abc",
        container_name="cr-sb-warm-ubuntu-1-abc",
        container_ip=ip,
        sandbox_type="ubuntu",
    )


def make_record(sandbox_id: str = "sb_abc123", status: str = "ready") -> SandboxRecord:
    return SandboxRecord(
        sandbox_id=sandbox_id,
        container_info=make_container(),
        user_id="u1",
        role_id="r1",
        status=status,
        acquired_at=datetime.now(tz=timezone.utc),
    )


@pytest.fixture
def app_with_mocks():
    """FastAPI app with mocked dependencies injected."""
    registry = MagicMock()
    registry.find_active = AsyncMock(return_value=None)
    registry.register = AsyncMock(return_value=make_record())
    registry.mark_released = AsyncMock(return_value=make_container())
    registry.get = AsyncMock(return_value=make_record())
    registry.list_all = MagicMock(return_value=[make_record()])

    warm_pool = MagicMock()
    warm_pool.acquire = AsyncMock(return_value=make_container())
    warm_pool.ensure_pool = AsyncMock()
    warm_pool.release = AsyncMock()

    container_manager = MagicMock()
    container_manager.run_container = AsyncMock(return_value=make_container())

    sandboxes_router.set_dependencies(registry, warm_pool, container_manager)
    proxy_router.set_registry(registry)

    app = FastAPI()
    app.include_router(sandboxes_router.router)
    from fastapi import APIRouter
    proxy_api = APIRouter(prefix="/v1/sandboxes")
    proxy_api.include_router(proxy_router.router)
    app.include_router(proxy_api)

    return app, registry, warm_pool, container_manager


@pytest.mark.asyncio
async def test_acquire_reuses_existing_sandbox(app_with_mocks):
    app, registry, warm_pool, _ = app_with_mocks
    existing = make_record("sb_existing")
    registry.find_active = AsyncMock(return_value=existing)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/v1/sandboxes/acquire", json={"user_id": "u1", "role_id": "r1"})

    assert resp.status_code == 200
    assert resp.json()["sandbox_id"] == "sb_existing"
    warm_pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_acquire_from_warm_pool(app_with_mocks):
    app, registry, warm_pool, _ = app_with_mocks
    registry.find_active = AsyncMock(return_value=None)
    warm_pool.acquire = AsyncMock(return_value=make_container())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/v1/sandboxes/acquire", json={"user_id": "u1", "role_id": "r1"})

    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_acquire_cold_start_when_pool_empty(app_with_mocks):
    app, registry, warm_pool, container_manager = app_with_mocks
    registry.find_active = AsyncMock(return_value=None)
    warm_pool.acquire = AsyncMock(return_value=None)  # pool empty

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/v1/sandboxes/acquire", json={"user_id": "u1", "role_id": "r1"})

    assert resp.status_code == 200
    container_manager.run_container.assert_awaited_once()
    # ensure_pool 通过 asyncio.create_task 后台启动，需让事件循环执行一次才能验证
    await asyncio.sleep(0)
    warm_pool.ensure_pool.assert_awaited_once()


@pytest.mark.asyncio
async def test_acquire_cold_start_failure_returns_503(app_with_mocks):
    app, registry, warm_pool, container_manager = app_with_mocks
    registry.find_active = AsyncMock(return_value=None)
    warm_pool.acquire = AsyncMock(return_value=None)
    container_manager.run_container.side_effect = RuntimeError("docker down")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/v1/sandboxes/acquire", json={"user_id": "u1", "role_id": "r1"})

    assert resp.status_code == 503
    assert "cold start failed" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_release_returns_ok(app_with_mocks):
    app, registry, warm_pool, _ = app_with_mocks

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/v1/sandboxes/sb_abc123/release")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


@pytest.mark.asyncio
async def test_release_unknown_sandbox_returns_404(app_with_mocks):
    app, registry, warm_pool, _ = app_with_mocks
    registry.mark_released = AsyncMock(return_value=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/v1/sandboxes/nonexistent/release")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_proxy_404_for_unknown_sandbox(app_with_mocks):
    app, registry, _, _ = app_with_mocks
    registry.get = AsyncMock(return_value=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/sandboxes/nonexistent/proxy/api/terminal/execute")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_proxy_409_for_released_sandbox(app_with_mocks):
    app, registry, _, _ = app_with_mocks
    released_record = make_record("sb_abc123", status="released")
    registry.get = AsyncMock(return_value=released_record)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/sandboxes/sb_abc123/proxy/api/terminal/execute")

    assert resp.status_code == 409
