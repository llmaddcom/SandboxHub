"""acquire 携带 env 注入的集成测试（issue #15/#16，mock 外部依赖，不连 Docker）。

验证：env 非空时绕过 warm pool 冷启动并把 extra_env 传给 run_container；
复用已有沙盒时忽略 env；无 env 时行为与现状一致（走 warm pool）。
"""
from datetime import datetime, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock
from httpx import AsyncClient, ASGITransport
from fastapi import FastAPI

from src.routers import sandboxes as sandboxes_router
from src.models import ContainerInfo, SandboxRecord


def _container(env_injected: bool = False) -> ContainerInfo:
    return ContainerInfo(
        container_id="cid", container_name="cr-sb", container_ip="172.17.0.9",
        sandbox_type="code", env_injected=env_injected,
    )


def _record() -> SandboxRecord:
    return SandboxRecord(
        sandbox_id="sb_env", container_info=_container(True), user_id="u1", role_id="r1",
        status="ready", acquired_at=datetime.now(tz=timezone.utc),
    )


@pytest.fixture
def app_mocks():
    registry = MagicMock()
    registry.find_active = AsyncMock(return_value=None)
    registry.register = AsyncMock(return_value=_record())

    warm_pool = MagicMock()
    warm_pool.acquire = AsyncMock(return_value=_container())
    warm_pool.ensure_pool = AsyncMock()

    cm = MagicMock()
    cm.run_container = AsyncMock(return_value=_container(env_injected=True))
    cm.is_healthy = AsyncMock(return_value=True)

    reconciler = MagicMock()
    reconciler.destroy_sandbox = AsyncMock()

    sandboxes_router.set_dependencies(registry, warm_pool, cm, reconciler)
    app = FastAPI()
    app.include_router(sandboxes_router.router)
    return app, registry, warm_pool, cm


_ENV_BODY = {
    "user_id": "u1", "role_id": "r1", "sandbox_type": "code",
    "env": {"CR_API_BASE": "http://host.docker.internal:8011", "CR_SANDBOX_TOKEN": "tk-secret"},
}


@pytest.mark.asyncio
async def test_acquire_with_env_bypasses_pool_and_injects(app_mocks):
    app, registry, warm_pool, cm = app_mocks

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post("/v1/sandboxes/acquire", json=_ENV_BODY)

    assert resp.status_code == 200
    assert resp.json()["sandbox_id"] == "sb_env"
    # 带 env：绕过 warm pool，冷启动并注入 extra_env
    warm_pool.acquire.assert_not_called()
    kwargs = cm.run_container.call_args.kwargs
    assert kwargs["extra_env"] == _ENV_BODY["env"]


@pytest.mark.asyncio
async def test_acquire_reuse_ignores_env(app_mocks):
    app, registry, warm_pool, cm = app_mocks
    registry.find_active = AsyncMock(return_value=_record())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post("/v1/sandboxes/acquire", json=_ENV_BODY)

    assert resp.status_code == 200
    # 复用已有沙盒：不冷启动、不取池，env 被忽略（issue 语义：首次注入持续有效）
    cm.run_container.assert_not_called()
    warm_pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_acquire_without_env_uses_pool(app_mocks):
    app, registry, warm_pool, cm = app_mocks

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post(
            "/v1/sandboxes/acquire",
            json={"user_id": "u1", "role_id": "r1", "sandbox_type": "code"},
        )

    assert resp.status_code == 200
    warm_pool.acquire.assert_awaited_once()
    cm.run_container.assert_not_called()
