"""SandboxReconciler 单元测试：启动恢复、周期对账、即时体检驱逐。

registry / warm_pool 用真实实现（纯内存），ContainerManager 与 close_client mock。
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import settings
from src.manager.reconciler import SandboxReconciler
from src.manager.registry import SandboxRegistry
from src.manager.warm_pool import WarmPool
from src.models import ContainerInfo, ManagedContainer


def make_managed(
    cid: str,
    status: str = "running",
    mounted: bool = False,
    ip: str = "172.17.0.5",
    age_seconds: float = 3600,
) -> ManagedContainer:
    return ManagedContainer(
        container_id=cid,
        container_name=f"cr-sb-{cid}",
        status=status,
        sandbox_type="code",
        mounted=mounted,
        created_at=datetime.now(tz=timezone.utc) - timedelta(seconds=age_seconds),
        container_ip=ip if status == "running" else "",
    )


def make_info(cid: str, ip: str = "172.17.0.5") -> ContainerInfo:
    return ContainerInfo(
        container_id=cid, container_name=f"cr-sb-{cid}",
        container_ip=ip, sandbox_type="code",
    )


@pytest.fixture
def mock_manager():
    m = MagicMock()
    m.list_managed = AsyncMock(return_value=[])
    m.remove_container = AsyncMock()
    m.is_healthy = AsyncMock(return_value=True)
    m.clean_and_reset = AsyncMock()
    return m


@pytest.fixture
def parts(mock_manager):
    registry = SandboxRegistry()
    pool = WarmPool(mock_manager)
    reconciler = SandboxReconciler(registry, pool, mock_manager)
    return registry, pool, reconciler, mock_manager


# ── startup ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_startup_removes_stopped_and_mounted(parts):
    registry, pool, reconciler, manager = parts
    manager.list_managed.return_value = [
        make_managed("c_exited", status="exited"),
        make_managed("c_mounted", mounted=True),
    ]
    await reconciler.startup()
    removed = {c.args[0] for c in manager.remove_container.await_args_list}
    assert removed == {"c_exited", "c_mounted"}
    assert pool.container_ids() == set()


@pytest.mark.asyncio
async def test_startup_adopts_healthy_warm_after_clean(parts):
    registry, pool, reconciler, manager = parts
    manager.list_managed.return_value = [make_managed("c_warm")]
    await reconciler.startup()
    # 收养前必须清理复位（防上一任租户串台）
    manager.clean_and_reset.assert_awaited_once_with("172.17.0.5")
    assert pool.container_ids() == {"c_warm"}
    manager.remove_container.assert_not_called()


@pytest.mark.asyncio
async def test_startup_destroys_unhealthy_or_unclean_warm(parts):
    registry, pool, reconciler, manager = parts
    manager.list_managed.return_value = [
        make_managed("c_dead", ip="172.17.0.5"),
        make_managed("c_dirty", ip="172.17.0.6"),
    ]
    manager.is_healthy = AsyncMock(side_effect=[False, True])
    manager.clean_and_reset = AsyncMock(side_effect=RuntimeError("reset failed"))
    await reconciler.startup()
    removed = {c.args[0] for c in manager.remove_container.await_args_list}
    assert removed == {"c_dead", "c_dirty"}
    assert pool.container_ids() == set()


# ── reconcile_once ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_forgets_and_removes_stopped_containers(parts):
    registry, pool, reconciler, manager = parts
    # 一个已分配、一个在池，容器都已停止
    record = await registry.register(make_info("c_alloc"), user_id="u1", role_id="r1")
    await pool.restore(make_info("c_pool", ip="172.17.0.6"))
    manager.list_managed.return_value = [
        make_managed("c_alloc", status="exited"),
        make_managed("c_pool", status="dead"),
    ]
    with patch("src.manager.reconciler.close_client", new_callable=AsyncMock):
        await reconciler.reconcile_once()
    assert await registry.get(record.sandbox_id) is None  # 驱逐 → 下次 acquire 重建
    assert pool.container_ids() == set()
    removed = {c.args[0] for c in manager.remove_container.await_args_list}
    assert removed == {"c_alloc", "c_pool"}


@pytest.mark.asyncio
async def test_reconcile_destroys_untracked_orphan_past_grace(parts):
    registry, pool, reconciler, manager = parts
    manager.list_managed.return_value = [
        make_managed("c_orphan_old", age_seconds=3600),
        make_managed("c_orphan_new", age_seconds=10),  # 创建宽限内（可能在途）
    ]
    with patch("src.manager.reconciler.close_client", new_callable=AsyncMock):
        await reconciler.reconcile_once()
    removed = {c.args[0] for c in manager.remove_container.await_args_list}
    assert removed == {"c_orphan_old"}


@pytest.mark.asyncio
async def test_reconcile_keeps_tracked_running_containers(parts):
    registry, pool, reconciler, manager = parts
    record = await registry.register(make_info("c_alloc"), user_id="u1", role_id="r1")
    await pool.restore(make_info("c_pool", ip="172.17.0.6"))
    manager.list_managed.return_value = [
        make_managed("c_alloc"),
        make_managed("c_pool", ip="172.17.0.6"),
    ]
    with patch("src.manager.reconciler.close_client", new_callable=AsyncMock):
        await reconciler.reconcile_once()
    manager.remove_container.assert_not_called()
    assert await registry.get(record.sandbox_id) is record
    assert pool.container_ids() == {"c_pool"}


@pytest.mark.asyncio
async def test_reconcile_evicts_record_of_vanished_container(parts):
    registry, pool, reconciler, manager = parts
    record = await registry.register(make_info("c_gone"), user_id="u1", role_id="r1")
    manager.list_managed.return_value = []  # 容器被手动 docker rm，Docker 侧已无
    with patch("src.manager.reconciler.close_client", new_callable=AsyncMock):
        await reconciler.reconcile_once()
    assert await registry.get(record.sandbox_id) is None
    assert await registry.find_active("u1", "r1") is None


@pytest.mark.asyncio
async def test_reconcile_releases_idle_sandbox(parts, monkeypatch):
    registry, pool, reconciler, manager = parts
    monkeypatch.setattr(settings, "SANDBOX_IDLE_TTL", 100)
    record = await registry.register(make_info("c_idle"), user_id="u1", role_id="r1")
    record.last_active_at = datetime.now(tz=timezone.utc) - timedelta(seconds=500)
    manager.list_managed.return_value = [make_managed("c_idle")]
    with patch("src.proxy.forwarder.close_client", new_callable=AsyncMock), \
         patch("src.manager.reconciler.close_client", new_callable=AsyncMock):
        await reconciler.reconcile_once()
    # 驱逐并走 pool.release：非挂载容器清理复位后回池
    assert await registry.get(record.sandbox_id) is None
    manager.clean_and_reset.assert_awaited_once_with("172.17.0.5")
    assert pool.container_ids() == {"c_idle"}


@pytest.mark.asyncio
async def test_reconcile_keeps_active_sandbox_within_ttl(parts, monkeypatch):
    registry, pool, reconciler, manager = parts
    monkeypatch.setattr(settings, "SANDBOX_IDLE_TTL", 100)
    record = await registry.register(make_info("c_busy"), user_id="u1", role_id="r1")
    manager.list_managed.return_value = [make_managed("c_busy")]
    with patch("src.manager.reconciler.close_client", new_callable=AsyncMock):
        await reconciler.reconcile_once()
    assert await registry.get(record.sandbox_id) is record


@pytest.mark.asyncio
async def test_reconcile_idle_ttl_disabled_by_zero(parts, monkeypatch):
    registry, pool, reconciler, manager = parts
    monkeypatch.setattr(settings, "SANDBOX_IDLE_TTL", 0)
    record = await registry.register(make_info("c_idle"), user_id="u1", role_id="r1")
    record.last_active_at = datetime.now(tz=timezone.utc) - timedelta(days=30)
    manager.list_managed.return_value = [make_managed("c_idle")]
    with patch("src.manager.reconciler.close_client", new_callable=AsyncMock):
        await reconciler.reconcile_once()
    assert await registry.get(record.sandbox_id) is record


@pytest.mark.asyncio
async def test_reconcile_prunes_stale_released_records(parts):
    registry, pool, reconciler, manager = parts
    record = await registry.register(make_info("c_rel"), user_id="u1", role_id="r1")
    await registry.mark_released(record.sandbox_id)
    record.last_active_at = datetime.now(tz=timezone.utc) - timedelta(seconds=700)
    with patch("src.manager.reconciler.close_client", new_callable=AsyncMock):
        await reconciler.reconcile_once()
    assert await registry.get(record.sandbox_id) is None


# ── evict_if_dead ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evict_if_dead_keeps_healthy_sandbox(parts):
    registry, pool, reconciler, manager = parts
    record = await registry.register(make_info("c_ok"), user_id="u1", role_id="r1")
    manager.is_healthy = AsyncMock(return_value=True)
    assert await reconciler.evict_if_dead(record.sandbox_id) is False
    assert await registry.get(record.sandbox_id) is record
    manager.remove_container.assert_not_called()


@pytest.mark.asyncio
async def test_evict_if_dead_destroys_unreachable_sandbox(parts):
    registry, pool, reconciler, manager = parts
    record = await registry.register(make_info("c_dead"), user_id="u1", role_id="r1")
    manager.is_healthy = AsyncMock(return_value=False)
    with patch("src.manager.reconciler.close_client", new_callable=AsyncMock) as mock_close:
        assert await reconciler.evict_if_dead(record.sandbox_id) is True
    assert await registry.get(record.sandbox_id) is None
    manager.remove_container.assert_awaited_once_with("c_dead")
    mock_close.assert_awaited_once_with("172.17.0.5")


@pytest.mark.asyncio
async def test_evict_if_dead_noop_for_unknown_sandbox(parts):
    registry, pool, reconciler, manager = parts
    assert await reconciler.evict_if_dead("nonexistent") is False
    manager.remove_container.assert_not_called()


# ── 镜像版本对账（issue #6）───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_version_drift_probes_once_per_container(parts, monkeypatch):
    _, _, reconciler, manager = parts
    manager.list_managed.return_value = [make_managed("c1")]
    monkeypatch.setattr(
        type(settings), "expected_app_version", property(lambda self: "2026.07.06")
    )
    reconciler._probe_app_version = AsyncMock(return_value="2026.06.24")

    await reconciler.check_version_drift()
    assert reconciler._version_checked == {"c1"}

    # 已记账的容器不再重复探测
    await reconciler.check_version_drift()
    reconciler._probe_app_version.assert_awaited_once()


@pytest.mark.asyncio
async def test_version_drift_skips_when_no_expected_version(parts, monkeypatch):
    _, _, reconciler, manager = parts
    monkeypatch.setattr(type(settings), "expected_app_version", property(lambda self: ""))
    await reconciler.check_version_drift()
    manager.list_managed.assert_not_awaited()


@pytest.mark.asyncio
async def test_version_drift_retries_unreachable_container(parts, monkeypatch):
    _, _, reconciler, manager = parts
    manager.list_managed.return_value = [make_managed("c1")]
    monkeypatch.setattr(
        type(settings), "expected_app_version", property(lambda self: "2026.07.06")
    )
    reconciler._probe_app_version = AsyncMock(return_value=None)  # 容器暂不可达

    await reconciler.check_version_drift()
    assert reconciler._version_checked == set()  # 未记账，下一轮重试

    reconciler._probe_app_version = AsyncMock(return_value="2026.07.06")
    await reconciler.check_version_drift()
    assert reconciler._version_checked == {"c1"}


@pytest.mark.asyncio
async def test_version_checked_pruned_when_container_gone(parts, monkeypatch):
    _, _, reconciler, manager = parts
    monkeypatch.setattr(
        type(settings), "expected_app_version", property(lambda self: "2026.07.06")
    )
    reconciler._version_checked = {"gone"}
    manager.list_managed.return_value = []
    await reconciler.check_version_drift()
    assert reconciler._version_checked == set()


@pytest.mark.asyncio
async def test_pool_entry_pruned_when_container_vanishes(parts):
    # 容器被 docker rm -f 彻底移除（docker 列表里完全消失）时，池记录须被摘除，
    # 否则 maintain 认为池是满的、不再补充
    _, pool, reconciler, manager = parts
    await pool.restore(make_info("gone"))
    manager.list_managed.return_value = []
    await reconciler.reconcile_once()
    assert pool.container_ids() == set()


@pytest.mark.asyncio
async def test_pool_entry_kept_when_container_alive(parts):
    _, pool, reconciler, manager = parts
    await pool.restore(make_info("alive"))
    manager.list_managed.return_value = [make_managed("alive")]
    await reconciler.reconcile_once()
    assert pool.container_ids() == {"alive"}
