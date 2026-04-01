# /data/zh/SandboxHub/tests/unit/test_sandboxhub_ping.py
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from src.models import ContainerInfo, SandboxRecord


def _make_app(registry, container_manager):
    """创建带注入依赖的测试 app"""
    from src.routers import sandboxes as sandboxes_router
    sandboxes_router.set_dependencies(registry, MagicMock(), container_manager)
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(sandboxes_router.router)
    return app


def _make_record(status: str = "ready") -> SandboxRecord:
    """构造测试用 SandboxRecord + ContainerInfo"""
    info = ContainerInfo(
        container_id="c1", container_name="n1",
        container_ip="10.0.0.1", sandbox_type="ubuntu",
    )
    return SandboxRecord(
        sandbox_id="sb_test", container_info=info,
        user_id="u1", role_id="r1", status=status,
        acquired_at=datetime.now(timezone.utc),
    )


def test_ping_sandbox_not_found():
    registry = AsyncMock()
    registry.get = AsyncMock(return_value=None)
    cm = MagicMock()
    client = TestClient(_make_app(registry, cm))
    resp = client.get("/v1/sandboxes/nonexistent/ping")
    assert resp.status_code == 404


def test_ping_sandbox_found_shallow():
    registry = AsyncMock()
    registry.get = AsyncMock(return_value=_make_record("ready"))
    cm = MagicMock()
    client = TestClient(_make_app(registry, cm))
    resp = client.get("/v1/sandboxes/sb_test/ping")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["status"] == "ready"
    assert data["container_ip"] == "10.0.0.1"
    assert "reachable" not in data


def test_ping_sandbox_shallow_not_ready():
    """浅检查时，sandbox 存在但 status 非 ready，ok 应为 False"""
    registry = AsyncMock()
    registry.get = AsyncMock(return_value=_make_record("releasing"))
    cm = MagicMock()
    client = TestClient(_make_app(registry, cm))
    resp = client.get("/v1/sandboxes/sb_test/ping")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["status"] == "releasing"
    assert "reachable" not in data


def test_ping_sandbox_deep_reachable():
    registry = AsyncMock()
    registry.get = AsyncMock(return_value=_make_record("ready"))
    cm = MagicMock()
    cm.is_healthy = AsyncMock(return_value=True)
    client = TestClient(_make_app(registry, cm))
    resp = client.get("/v1/sandboxes/sb_test/ping?deep=true")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["reachable"] is True


def test_ping_sandbox_deep_unreachable():
    registry = AsyncMock()
    registry.get = AsyncMock(return_value=_make_record("ready"))
    cm = MagicMock()
    cm.is_healthy = AsyncMock(return_value=False)
    client = TestClient(_make_app(registry, cm))
    resp = client.get("/v1/sandboxes/sb_test/ping?deep=true")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["reachable"] is False
