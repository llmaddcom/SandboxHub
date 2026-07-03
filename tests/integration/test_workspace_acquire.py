"""acquire 携带 workspace 的集成测试（FastAPI TestClient，mock 外部依赖，不连 Docker/MinIO）。

验证：mount_ready 时挂载请求走专属冷启动（带 workspace、绕过 warm pool）；
不就绪时记 warning 回退为普通 warm-pool 路径。
"""
from datetime import datetime, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock
from httpx import AsyncClient, ASGITransport
from fastapi import FastAPI

from src.config import settings
from src.routers import sandboxes as sandboxes_router
from src.models import ContainerInfo, SandboxRecord


def _container(mounted: bool = False) -> ContainerInfo:
    return ContainerInfo(
        container_id="cid", container_name="cr-sb", container_ip="172.17.0.9",
        sandbox_type="code", mounted=mounted,
    )


def _record() -> SandboxRecord:
    return SandboxRecord(
        sandbox_id="sb_x", container_info=_container(True), user_id="u1", role_id="r1",
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
    cm.run_container = AsyncMock(return_value=_container(mounted=True))
    cm.is_healthy = AsyncMock(return_value=True)

    reconciler = MagicMock()
    reconciler.destroy_sandbox = AsyncMock()

    sandboxes_router.set_dependencies(registry, warm_pool, cm, reconciler)
    app = FastAPI()
    app.include_router(sandboxes_router.router)
    return app, registry, warm_pool, cm


_WS_BODY = {
    "user_id": "u1", "role_id": "r1", "sandbox_type": "code",
    "workspace": {"bucket": "cr-ws", "prefix": "roles/r1", "mount_path": "/workspace"},
}


@pytest.mark.asyncio
async def test_acquire_with_workspace_mounts_and_bypasses_pool(app_mocks, monkeypatch):
    app, registry, warm_pool, cm = app_mocks
    # 让 settings.mount_ready 为真
    monkeypatch.setattr(settings, "WORKSPACE_MOUNT_ENABLED", True)
    monkeypatch.setattr(settings, "MINIO_ENDPOINT", "minio:9000")
    monkeypatch.setattr(settings, "MINIO_ACCESS_KEY", "ak")
    monkeypatch.setattr(settings, "MINIO_SECRET_KEY", "sk")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post("/v1/sandboxes/acquire", json=_WS_BODY)

    assert resp.status_code == 200
    assert resp.json()["sandbox_id"] == "sb_x"
    # 走专属挂载冷启动：run_container 带 workspace，且未从 warm pool 取
    warm_pool.acquire.assert_not_called()
    kwargs = cm.run_container.call_args.kwargs
    assert kwargs["workspace"].bucket == "cr-ws"
    assert kwargs["workspace"].prefix == "roles/r1"


@pytest.mark.asyncio
async def test_acquire_with_workspace_falls_back_when_not_ready(app_mocks, monkeypatch):
    app, registry, warm_pool, cm = app_mocks
    # 缺 MinIO 凭据 → mount_ready 为假 → 回退普通 warm-pool 路径，忽略 workspace
    monkeypatch.setattr(settings, "WORKSPACE_MOUNT_ENABLED", True)
    monkeypatch.setattr(settings, "MINIO_ENDPOINT", "")
    monkeypatch.setattr(settings, "MINIO_ACCESS_KEY", "")
    monkeypatch.setattr(settings, "MINIO_SECRET_KEY", "")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post("/v1/sandboxes/acquire", json=_WS_BODY)

    assert resp.status_code == 200
    # 回退：从 warm pool 取，run_container 未以挂载方式被调用
    warm_pool.acquire.assert_awaited_once()
    cm.run_container.assert_not_called()


@pytest.mark.asyncio
async def test_acquire_without_workspace_uses_pool(app_mocks, monkeypatch):
    app, registry, warm_pool, cm = app_mocks
    monkeypatch.setattr(settings, "MINIO_ENDPOINT", "minio:9000")
    monkeypatch.setattr(settings, "MINIO_ACCESS_KEY", "ak")
    monkeypatch.setattr(settings, "MINIO_SECRET_KEY", "sk")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post(
            "/v1/sandboxes/acquire",
            json={"user_id": "u1", "role_id": "r1", "sandbox_type": "code"},
        )

    assert resp.status_code == 200
    warm_pool.acquire.assert_awaited_once()
    cm.run_container.assert_not_called()
