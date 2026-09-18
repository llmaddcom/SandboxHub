"""网络策略（SandboxHub#42）：策略解析 / 热切网 / 网关 / 批量应用 / 归还切回 / 对账对齐。

registry / warm_pool 用真实实现（纯内存），ContainerManager 与 Docker SDK mock。
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import load_settings, settings
from src.manager.container_manager import ContainerManager
from src.manager.reconciler import SandboxReconciler
from src.manager.registry import SandboxRegistry
from src.manager.warm_pool import WarmPool
from src.models import ContainerInfo, ManagedContainer, SandboxRecord

NET = "cr-sb-net"
ISO = "cr-sb-isolated"


@pytest.fixture(autouse=True)
def _policy_supported(monkeypatch):
    """默认按「用户自定义网络」跑（.env 里可能仍是 bridge）。"""
    monkeypatch.setattr(settings, "SANDBOX_NETWORK", NET)
    monkeypatch.setattr(settings, "SANDBOX_NETWORK_ISOLATED", ISO)
    monkeypatch.setattr(settings, "SANDBOX_GATEWAY_NAME", "cr-host")


def make_info(cid: str = "c1", ip: str = "10.0.0.1", network: str = NET) -> ContainerInfo:
    return ContainerInfo(
        container_id=cid, container_name=f"cr-sb-{cid}", container_ip=ip,
        sandbox_type="code", network=network,
    )


def make_record(info: ContainerInfo, user: str = "u1", role: str = "r1") -> SandboxRecord:
    return SandboxRecord(
        sandbox_id=f"sb_{info.container_id}", container_info=info, user_id=user, role_id=role,
        status="ready", acquired_at=datetime.now(timezone.utc),
    )


class FakeSwitchManager:
    """switch_network 的可控替身：按 mode 改 info 的 network/ip，可指定失败。"""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple[str, str]] = []
        self.fail = fail
        self.is_healthy = AsyncMock(return_value=True)
        self.remove_container = AsyncMock()
        self.run_container = AsyncMock(side_effect=self._run)

    async def switch_network(self, info: ContainerInfo, mode: str) -> ContainerInfo:
        self.calls.append((info.container_id, mode))
        if self.fail:
            raise RuntimeError("boom")
        target = settings.network_for_mode(mode)
        if info.network != target:
            info.network = target
            info.container_ip = "10.9.0.1" if mode == "deny" else "10.0.0.9"
        return info

    async def _run(self, sandbox_type, slot=0, workspace=None, extra_env=None, network=None):
        return make_info("new", "10.1.0.1", network or settings.SANDBOX_NETWORK)


def make_app(registry, manager, reconciler=None, pool=None):
    from src.routers import sandboxes as router_mod
    pool = pool or MagicMock(acquire=AsyncMock(return_value=None), ensure_pool=AsyncMock())
    router_mod.set_dependencies(registry, pool, manager, reconciler or MagicMock(destroy_sandbox=AsyncMock()))
    app = FastAPI()
    app.include_router(router_mod.router)
    return app


# ── config ───────────────────────────────────────────────────────────────────

def test_config_policy_support_and_network_for_mode():
    s = load_settings(env_file=None, SANDBOX_NETWORK="cr-sb-net", SANDBOX_NETWORK_ISOLATED="iso")
    assert s.network_policy_supported is True
    assert s.network_for_mode("deny") == "iso" and s.network_for_mode("allow") == "cr-sb-net"
    for builtin in ("bridge", "host", "none"):
        assert load_settings(env_file=None, SANDBOX_NETWORK=builtin).network_policy_supported is False


def test_config_gateway_forwards_and_container_endpoint():
    s = load_settings(
        env_file=None, SANDBOX_NETWORK="cr-sb-net", SANDBOX_GATEWAY_NAME="cr-host",
        MINIO_ENDPOINT="172.17.0.1:9000",
        SANDBOX_GATEWAY_FORWARDS="8012=host.docker.internal:8012, bad, 70000=x:1, 9000=other:9000",
    )
    # MinIO 自动加入且可被同端口显式条目覆盖；坏条目跳过
    assert s.gateway_forwards() == [(8012, "host.docker.internal:8012"), (9000, "other:9000")]
    assert s.minio_endpoint_for_container() == "cr-host:9000"
    assert s.minio_rclone_env()["RCLONE_CONFIG_MINIO_ENDPOINT"] == "http://cr-host:9000"
    # 内置 bridge：不走网关，原值
    legacy = load_settings(env_file=None, SANDBOX_NETWORK="bridge", MINIO_ENDPOINT="172.17.0.1:9000")
    assert legacy.minio_endpoint_for_container() == "172.17.0.1:9000"
    assert legacy.gateway_forwards() == [(9000, "172.17.0.1:9000")]


# ── acquire 策略解析与落实 ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_acquire_reuse_switches_to_isolated_and_echoes_effective_policy():
    registry = SandboxRegistry()
    info = make_info()
    await registry.register(info, "u1", "r1")
    manager = FakeSwitchManager()
    client = TestClient(make_app(registry, manager))
    resp = client.post("/v1/sandboxes/acquire", json={
        "user_id": "u1", "role_id": "r1", "sandbox_type": "code",
        "policy": {"network": {"default": "deny"}, "protected_paths": ["Uploads"]},
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["effective_policy"]["network"]["default"] == "deny"
    assert body["effective_policy"]["protected_paths"] == ["Uploads"]
    assert any("protected_paths" in r for r in body["effective_policy"]["reasons"])
    assert manager.calls == [("c1", "deny")]
    assert info.network == ISO and info.container_ip == "10.9.0.1"


@pytest.mark.asyncio
async def test_acquire_without_policy_is_allow_and_still_reconciles_network():
    registry = SandboxRegistry()
    info = make_info(network=ISO, ip="10.9.0.1")  # 上次被切到隔离网
    await registry.register(info, "u1", "r1")
    manager = FakeSwitchManager()
    client = TestClient(make_app(registry, manager))
    resp = client.post("/v1/sandboxes/acquire", json={"user_id": "u1", "role_id": "r1", "sandbox_type": "code"})
    assert resp.status_code == 200
    assert resp.json()["effective_policy"] == {
        "schema_version": 1, "network": {"default": "allow", "allow": [], "deny": []},
        "protected_paths": [], "writable_roots": [], "reasons": [],
    }
    assert manager.calls == [("c1", "allow")] and info.network == NET


def test_acquire_rejects_allow_deny_lists():
    client = TestClient(make_app(SandboxRegistry(), FakeSwitchManager()))
    resp = client.post("/v1/sandboxes/acquire", json={
        "user_id": "u1", "role_id": "r1", "policy": {"network": {"default": "deny", "allow": ["*.pypi.org"]}},
    })
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "network_policy_lists_unsupported"


def test_acquire_rejects_deny_when_network_policy_unsupported(monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_NETWORK", "bridge")
    client = TestClient(make_app(SandboxRegistry(), FakeSwitchManager()))
    resp = client.post("/v1/sandboxes/acquire", json={
        "user_id": "u1", "role_id": "r1", "policy": {"network": {"default": "deny"}},
    })
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "network_policy_unsupported"
    # allow 在内置网络上照常可用（与旧版一致）
    resp = client.post("/v1/sandboxes/acquire", json={"user_id": "u1", "role_id": "r1", "sandbox_type": "code"})
    assert resp.status_code == 200 and resp.json()["effective_policy"]["network"]["default"] == "allow"


@pytest.mark.asyncio
async def test_acquire_reuse_switch_failure_destroys_and_cold_starts_on_target_network():
    registry = SandboxRegistry()
    old = make_info()
    record = await registry.register(old, "u1", "r1")
    manager = FakeSwitchManager(fail=True)
    reconciler = MagicMock()

    async def destroy(rec):
        await registry.evict(rec.sandbox_id)
    reconciler.destroy_sandbox = AsyncMock(side_effect=destroy)
    client = TestClient(make_app(registry, manager, reconciler))
    resp = client.post("/v1/sandboxes/acquire", json={
        "user_id": "u1", "role_id": "r1", "sandbox_type": "code", "policy": {"network": {"default": "deny"}},
    })
    assert resp.status_code == 200
    reconciler.destroy_sandbox.assert_awaited_once()
    assert reconciler.destroy_sandbox.await_args.args[0].sandbox_id == record.sandbox_id
    manager.run_container.assert_awaited_once()
    assert manager.run_container.await_args.kwargs["network"] == ISO
    assert resp.json()["sandbox_id"] != record.sandbox_id


@pytest.mark.asyncio
async def test_acquire_from_pool_switches_and_discards_unswitchable():
    registry = SandboxRegistry()
    manager = FakeSwitchManager()
    good = make_info("pool2", "10.0.0.2")
    bad = make_info("pool1", "10.0.0.3")
    pool = MagicMock(acquire=AsyncMock(side_effect=[bad, good]), ensure_pool=AsyncMock())
    orig = manager.switch_network

    async def switch(info, mode):
        if info.container_id == "pool1":
            raise RuntimeError("stuck")
        return await orig(info, mode)
    manager.switch_network = switch
    client = TestClient(make_app(registry, manager, pool=pool))
    resp = client.post("/v1/sandboxes/acquire", json={
        "user_id": "u1", "role_id": "r1", "sandbox_type": "code", "policy": {"network": {"default": "deny"}},
    })
    assert resp.status_code == 200
    manager.remove_container.assert_awaited_once_with("pool1")
    rec = await registry.get(resp.json()["sandbox_id"])
    assert rec.container_info is good and good.network == ISO


# ── policy/apply ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_policy_apply_switches_matching_ready_sandboxes_and_destroys_failures():
    registry = SandboxRegistry()
    a = make_info("a", "10.0.0.1"); b = make_info("b", "10.0.0.2"); c = make_info("c", "10.0.0.3")
    await registry.register(a, "u1", "r1")
    await registry.register(b, "u1", "r2")
    await registry.register(c, "u2", "r1")
    manager = FakeSwitchManager()
    orig = manager.switch_network

    async def switch(info, mode):
        if info.container_id == "b":
            raise RuntimeError("stuck")
        return await orig(info, mode)
    manager.switch_network = switch
    reconciler = MagicMock(destroy_sandbox=AsyncMock())
    client = TestClient(make_app(registry, manager, reconciler))
    resp = client.post("/v1/sandboxes/policy/apply", json={"policy": {"network": {"default": "deny"}}, "user_id": "u1"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    sid = {r.container_info.container_id: r.sandbox_id for r in registry.list_ready()}
    assert [s["sandbox_id"] for s in body["switched"]] == [sid["a"]]
    assert body["failed"] == [{"sandbox_id": sid["b"], "error": "network switch failed"}]
    assert body["effective_policy"]["network"]["default"] == "deny"
    reconciler.destroy_sandbox.assert_awaited_once()
    assert a.network == ISO and c.network == NET  # u2 未过滤到，不动
    # 已在目标网络的沙盒再 apply 不计入 switched
    resp = client.post("/v1/sandboxes/policy/apply", json={"policy": {"network": {"default": "deny"}}, "user_id": "u1"})
    assert resp.json()["switched"] == []


# ── ContainerManager：切网 / 网关 / 标记 ─────────────────────────────────────

@pytest.fixture
def mock_docker():
    with patch("src.manager.container_manager.docker") as mock:
        client = MagicMock()
        mock.from_env.return_value = client
        import docker.errors
        mock.errors = docker.errors
        yield client


def _container_on(networks: dict[str, str]):
    c = MagicMock()
    c.id = "cid"; c.name = "cr-sb-x"; c.status = "running"
    c.attrs = {"NetworkSettings": {"Networks": {n: {"IPAddress": ip} for n, ip in networks.items()}}}
    return c


def test_switch_network_sync_disconnects_others_and_connects_target(mock_docker):
    manager = ContainerManager()
    container = _container_on({NET: "10.0.0.5"})
    nets = {NET: MagicMock(), ISO: MagicMock()}
    mock_docker.networks.get.side_effect = lambda name: nets[name]
    mock_docker.containers.get.return_value = container

    # 第一次 reload 看到旧网，connect 之后的 reload 看到新网
    def reload():
        if nets[ISO].connect.called:
            container.attrs = {"NetworkSettings": {"Networks": {ISO: {"IPAddress": "10.9.0.5"}}}}
    container.reload.side_effect = reload
    ip = manager._switch_network_sync("cid", ISO)
    nets[NET].disconnect.assert_called_once_with(container, force=True)
    nets[ISO].connect.assert_called_once_with(container)
    assert ip == "10.9.0.5"


@pytest.mark.asyncio
async def test_switch_network_noop_when_already_on_target(mock_docker):
    manager = ContainerManager()
    manager._switch_network_sync = MagicMock()
    info = make_info(network=ISO, ip="10.9.0.1")
    assert await manager.switch_network(info, "deny") is info
    manager._switch_network_sync.assert_not_called()


@pytest.mark.asyncio
async def test_switch_network_updates_info_and_writes_marker(mock_docker):
    manager = ContainerManager()
    manager._switch_network_sync = MagicMock(return_value="10.9.0.7")
    manager._write_policy_marker_sync = MagicMock()
    manager.wait_healthy = AsyncMock(return_value=True)
    info = make_info(network=NET, ip="10.0.0.7")
    await manager.switch_network(info, "deny")
    manager._switch_network_sync.assert_called_once_with("c1", ISO)
    manager._write_policy_marker_sync.assert_called_once_with("c1", "deny")
    assert info.network == ISO and info.container_ip == "10.9.0.7"


@pytest.mark.asyncio
async def test_switch_network_raises_when_unreachable_after_switch(mock_docker):
    manager = ContainerManager()
    manager._switch_network_sync = MagicMock(return_value="10.9.0.7")
    manager.wait_healthy = AsyncMock(return_value=False)
    with pytest.raises(RuntimeError, match="切网后容器不可达"):
        await manager.switch_network(make_info(), "deny")


@pytest.mark.asyncio
async def test_run_container_on_isolated_writes_marker(mock_docker):
    manager = ContainerManager()
    manager._run_container_sync = MagicMock(return_value=("cid", "10.9.0.2"))
    manager.wait_healthy = AsyncMock(return_value=True)
    manager._write_policy_marker_sync = MagicMock()
    info = await manager.run_container("code", network=ISO)
    assert info.network == ISO and info.container_ip == "10.9.0.2"
    assert manager._run_container_sync.call_args.args[-1] == ISO
    manager._write_policy_marker_sync.assert_called_once_with("cid", "deny")
    manager._write_policy_marker_sync.reset_mock()
    info = await manager.run_container("code")
    assert info.network == NET
    manager._write_policy_marker_sync.assert_not_called()


def test_ensure_gateway_creates_and_attaches_both_networks(mock_docker, monkeypatch):
    import docker.errors
    monkeypatch.setattr(settings, "MINIO_ENDPOINT", "172.17.0.1:9000")
    monkeypatch.setattr(settings, "SANDBOX_GATEWAY_FORWARDS", "8012=host.docker.internal:8012")
    manager = ContainerManager()
    mock_docker.containers.get.side_effect = docker.errors.NotFound("no")
    created = _container_on({NET: "10.0.0.100"})
    mock_docker.containers.run.return_value = created
    iso_net = MagicMock()
    mock_docker.networks.get.return_value = iso_net
    manager._ensure_gateway_sync()
    kwargs = mock_docker.containers.run.call_args.kwargs
    assert kwargs["name"] == "cr-host" and kwargs["network"] == NET
    assert kwargs["environment"] == {"FORWARDS": "8012=host.docker.internal:8012,9000=172.17.0.1:9000"}
    assert kwargs["labels"] == {"sandboxhub.gateway": "true"}
    assert "sandboxhub.managed" not in kwargs["labels"]  # 不是受管沙盒，不参与对账
    iso_net.connect.assert_called_once_with(created)


def test_ensure_gateway_rebuilds_when_forwards_changed(mock_docker, monkeypatch):
    monkeypatch.setattr(settings, "MINIO_ENDPOINT", "172.17.0.1:9000")
    monkeypatch.setattr(settings, "SANDBOX_GATEWAY_FORWARDS", "")
    manager = ContainerManager()
    stale = _container_on({NET: "10.0.0.100", ISO: "10.9.0.100"})
    stale.labels = {"sandboxhub.gateway": "true"}
    stale.image.tags = [settings.DOCKER_IMAGE_GATEWAY]
    stale.attrs["Config"] = {"Env": ["FORWARDS=9000=old:9000"], "Image": settings.DOCKER_IMAGE_GATEWAY}
    mock_docker.containers.get.return_value = stale
    fresh = _container_on({NET: "10.0.0.101", ISO: "10.9.0.101"})
    mock_docker.containers.run.return_value = fresh
    manager._ensure_gateway_sync()
    stale.remove.assert_called_once_with(force=True)
    assert mock_docker.containers.run.call_args.kwargs["environment"] == {"FORWARDS": "9000=172.17.0.1:9000"}
    mock_docker.networks.get.assert_not_called()  # 新容器已在两张网上


@pytest.mark.asyncio
async def test_ensure_networks_skipped_on_builtin_bridge(mock_docker, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_NETWORK", "bridge")
    manager = ContainerManager()
    await manager.ensure_networks()
    await manager.ensure_gateway()
    mock_docker.networks.create.assert_not_called()
    mock_docker.containers.run.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_networks_creates_missing(mock_docker):
    import docker.errors
    manager = ContainerManager()
    mock_docker.networks.get.side_effect = docker.errors.NotFound("no")
    await manager.ensure_networks()
    calls = {c.args[0]: c.kwargs["internal"] for c in mock_docker.networks.create.call_args_list}
    assert calls == {NET: False, ISO: True}


# ── warm pool：归还前切回联网态 ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_release_switches_isolated_container_back_before_pooling():
    manager = FakeSwitchManager()
    manager.clean_and_reset = AsyncMock()
    pool = WarmPool(manager)  # type: ignore[arg-type]
    info = make_info(network=ISO, ip="10.9.0.1")
    with patch("src.proxy.forwarder.close_client", new_callable=AsyncMock):
        await pool.release(info)
    assert manager.calls == [("c1", "allow")]
    assert info.network == NET
    manager.clean_and_reset.assert_awaited_once_with(info.container_ip)
    assert pool.available_count("code") == 1


@pytest.mark.asyncio
async def test_release_destroys_when_switch_back_fails():
    manager = FakeSwitchManager(fail=True)
    manager.clean_and_reset = AsyncMock()
    pool = WarmPool(manager)  # type: ignore[arg-type]
    await pool.release(make_info(network=ISO, ip="10.9.0.1"))
    manager.remove_container.assert_awaited_once_with("c1")
    manager.clean_and_reset.assert_not_awaited()
    assert pool.available_count("code") == 0


# ── reconciler：在册网络 / IP 与 Docker 实际对齐 ──────────────────────────────

@pytest.mark.asyncio
async def test_reconcile_aligns_network_and_ip_with_docker(monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_IDLE_TTL", 0)
    registry = SandboxRegistry()
    info = make_info(network=NET, ip="10.0.0.1")
    record = await registry.register(info, "u1", "r1")
    manager = MagicMock()
    actual = ManagedContainer(
        container_id="c1", container_name="cr-sb-c1", status="running", sandbox_type="code",
        mounted=False, created_at=datetime.now(timezone.utc), container_ip="10.9.0.1", network=ISO,
    )
    manager.list_managed = AsyncMock(return_value=[actual])
    manager.remove_container = AsyncMock()
    pool = WarmPool(manager)
    with patch("src.manager.reconciler.close_client", new_callable=AsyncMock) as close:
        await SandboxReconciler(registry, pool, manager).reconcile_once()
    assert record.container_info.network == ISO and record.container_info.container_ip == "10.9.0.1"
    close.assert_awaited_once_with("10.0.0.1")
