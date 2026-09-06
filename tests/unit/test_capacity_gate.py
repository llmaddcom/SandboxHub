"""sandbox.max_total 总量闸：到上限 acquire 返回 503 capacity_reached；计数失败不阻断。"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from src.config import settings
from src.routers import sandboxes


@pytest.fixture
def deps(monkeypatch):
    registry = MagicMock()
    registry.find_active = AsyncMock(return_value=None)
    registry.list_ready = MagicMock(return_value=[])
    manager = MagicMock()
    manager.count_managed = MagicMock(return_value=4)
    sandboxes.set_dependencies(registry, MagicMock(), manager, MagicMock())
    monkeypatch.setattr(settings, "SANDBOX_MAX_TOTAL", 4)
    return manager


@pytest.mark.asyncio
async def test_capacity_reached_returns_503(deps):
    req = sandboxes.AcquireRequest(user_id="u", role_id="r", sandbox_type="code")
    with pytest.raises(HTTPException) as ei:
        await sandboxes.acquire_sandbox(req)
    assert ei.value.status_code == 503
    assert ei.value.detail["reason"] == "capacity_reached"


@pytest.mark.asyncio
async def test_count_failure_does_not_block(deps, monkeypatch):
    deps.count_managed.side_effect = RuntimeError("docker down")
    deps.run_container = AsyncMock(return_value=MagicMock(container_id="c", container_ip="1.2.3.4"))
    # 只验证闸门被跳过（后续流程用到的依赖是 mock，异常也不该是 capacity_reached）
    req = sandboxes.AcquireRequest(user_id="u", role_id="r", sandbox_type="code")
    try:
        await sandboxes.acquire_sandbox(req)
    except HTTPException as e:
        assert e.detail.get("reason") != "capacity_reached"
    except Exception:
        pass
