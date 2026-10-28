"""acquire 携带 workspace 的集成测试（FastAPI TestClient，mock 外部依赖，不连 Docker/MinIO）。

验证：mount_ready 时挂载请求走专属冷启动（带 workspace、绕过 warm pool）；
不就绪时记 warning 回退为普通 warm-pool 路径。
"""
from datetime import datetime, timezone
import hashlib
import json

import pytest
from unittest.mock import AsyncMock, MagicMock
from httpx import AsyncClient, ASGITransport
from fastapi import FastAPI

from src.config import settings
from src.routers import sandboxes as sandboxes_router
from src.models import ContainerInfo, SandboxRecord
from src.manager.inputs_cache import InputsCache


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
    # 复用 / 出池都会按策略热切网（#42），失败时销毁容器；替身切网原地成功
    cm.switch_network = AsyncMock()
    cm.remove_container = AsyncMock()

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


def _input():
    return {"path": "version-a/report.txt", "bucket": "files", "key": "file-center/u1/version-a/report.txt",
            "sha256": hashlib.sha256(b"hello").hexdigest(), "size": 5}


@pytest.mark.asyncio
async def test_file_center_manifest_is_readonly_scoped_and_revocable(tmp_path, monkeypatch):
    class Response:
        def stream(self, _size):
            yield b"hello"
        def close(self):
            pass
        def release_conn(self):
            pass
    client = MagicMock()
    client.get_object.return_value = Response()
    cache = InputsCache(tmp_path, client_factory=lambda: client)
    monkeypatch.setattr(settings, "WORK_AREA_ROOT", str(tmp_path))
    monkeypatch.setattr(sandboxes_router, "_inputs_cache", lambda _root: cache)
    request = sandboxes_router.AcquireRequest(user_id="u1", role_id="r1", inputs=[_input()])
    from pathlib import Path
    view = Path(await sandboxes_router._prepare_inputs(request))
    assert (view / "version-a/report.txt").read_bytes() == b"hello"
    assert (view / "version-a/report.txt").stat().st_mode & 0o777 == 0o444
    index = json.loads((view / "INDEX.json").read_text())
    assert index == [{"path": "version-a/report.txt", "size_bytes": 5, "sha256": _input()["sha256"]}]
    assert not any(key in index[0] for key in ("bucket", "key"))
    request.inputs = []
    assert await sandboxes_router._prepare_inputs(request) == str(view)
    assert not (view / "version-a/report.txt").exists()
    assert json.loads((view / "INDEX.json").read_text()) == []
    request.inputs = [{**_input(), "key": "file-center/other/version-a/report.txt"}]
    with pytest.raises(ValueError, match="不属于"):
        await sandboxes_router._prepare_inputs(request)


@pytest.mark.asyncio
async def test_acquire_file_inputs_cold_starts_with_readonly_mount(app_mocks, monkeypatch):
    app, registry, warm_pool, cm = app_mocks
    container = _container()
    container.inputs_mounted = True
    container.env_injected = True
    cm.run_container.return_value = container
    prepare = AsyncMock(return_value="/host/u1/r1/inputs")
    monkeypatch.setattr(sandboxes_router, "_prepare_inputs", prepare)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post("/v1/sandboxes/acquire", json={"user_id": "u1", "role_id": "r1", "inputs": [_input()]})
    assert response.status_code == 200 and response.json()["inputs_ready"]
    assert cm.run_container.call_args.kwargs["inputs_path"] == "/host/u1/r1/inputs"
    warm_pool.acquire.assert_not_called()
    assert not sandboxes_router._input_locks


@pytest.mark.asyncio
@pytest.mark.parametrize("idle", [False, None])
async def test_inputs_upgrade_preserves_busy_existing_jobs(app_mocks, monkeypatch, idle):
    app, registry, warm_pool, cm = app_mocks
    registry.find_active.return_value = _record()
    reconciler = sandboxes_router._reconciler
    reconciler._probe_terminal_idle = AsyncMock(return_value=idle)
    prepare = AsyncMock()
    monkeypatch.setattr(sandboxes_router, "_prepare_inputs", prepare)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post("/v1/sandboxes/acquire", json={"user_id": "u1", "role_id": "r1", "inputs": [_input()]})
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "inputs_mount_pending"
    reconciler.destroy_sandbox.assert_not_called()
    prepare.assert_not_called()
    cm.run_container.assert_not_called()


@pytest.mark.asyncio
async def test_inputs_manifest_refresh_reuses_mounted_container(app_mocks, monkeypatch):
    app, registry, warm_pool, cm = app_mocks
    record = _record()
    record.container_info.inputs_mounted = True
    registry.find_active.return_value = record
    prepare = AsyncMock(return_value="/host/u1/r1/inputs")
    monkeypatch.setattr(sandboxes_router, "_prepare_inputs", prepare)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post("/v1/sandboxes/acquire", json={"user_id": "u1", "role_id": "r1", "inputs": []})
    assert response.status_code == 200 and response.json()["inputs_ready"]
    prepare.assert_awaited_once()
    sandboxes_router._reconciler.destroy_sandbox.assert_not_called()
    cm.run_container.assert_not_called()
