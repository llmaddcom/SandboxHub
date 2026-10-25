"""WorkAreaService 编排：分配 / 复用 / 重建 / 忙碌 / 上限 / 失败回滚 / 释放 / 重启收养 / 对账。

ContainerManager 与 cr-fs 用假实现（不碰 Docker）；工作区与实例目录、inputs 缓存用真实实现落在 tmp。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import settings
from src.manager.fs_sidecar import FsOpError
from src.manager.inputs_cache import InputsCache
from src.manager.netguard import Capabilities, Capability, ImageFeatures
from src.manager.wa_instances import AcquireSpec, WorkAreaError, WorkAreaService
from src.models import (
    MOUNT_LAYOUT_LABEL,
    ROLE_LABEL,
    SANDBOX_ID_LABEL,
    USER_LABEL,
    V2_LABEL,
    WORK_AREA_LABEL,
    ContainerInfo,
    InputFileSpec,
    ManagedContainer,
    RoleMountSpec,
)

SKILLS = RoleMountSpec("skills", "bkt", "roles/r1/skills/", "ro")
NOTES = RoleMountSpec("notes", "bkt", "roles/r1/notes/", "rw")


class FakeSidecar:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.name = "cr-test-fs"
        self.mounted: dict[str, bool] = {}
        self.mount_calls: list[str] = []
        self.unmount_calls: list[str] = []
        self.ensure = AsyncMock(return_value=False)
        self.running = AsyncMock(return_value=True)
        self.fsops = AsyncMock(return_value={"removed": True})
        self.exec = AsyncMock(return_value=(0, b"", b""))

    def host_path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    @staticmethod
    def container_path(*parts: str) -> str:
        return "/".join(("/srv", *parts))

    async def mount_status(self) -> dict[str, bool]:
        return dict(self.mounted)

    async def mount(self, spec: RoleMountSpec) -> None:
        self.mount_calls.append(spec.key)
        self.mounted[spec.key] = True

    async def unmount(self, key: str) -> None:
        self.unmount_calls.append(key)
        self.mounted.pop(key, None)


class FakeMinio:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def get_object(self, bucket, key):
        data = self.objects[(bucket, key)]

        class R:
            def stream(self, n):
                yield data

            def close(self):
                pass

            def release_conn(self):
                pass

        return R()


def _ok_caps() -> Capabilities:
    items = {name: Capability(True, "ok") for name in Capabilities.REQUIRED}
    items.update(image_code=Capability(True), image_ubuntu=Capability(True))
    return Capabilities(items=items, checked_at=datetime.now(tz=timezone.utc))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "WORK_AREA_ENABLED", True)
    monkeypatch.setattr(settings, "WORK_AREA_MAX_PER_ROLE", 2)
    monkeypatch.setattr(settings, "WORK_AREA_MAX_PER_USER", 4)
    monkeypatch.setattr(settings, "ORPHAN_GRACE_SECONDS", 60)
    manager = MagicMock()
    manager.ensure_wa_networks = AsyncMock()
    manager.ensure_wa_gateway = AsyncMock(return_value=False)
    manager.list_managed = AsyncMock(return_value=[])
    manager.is_healthy = AsyncMock(return_value=True)
    manager.remove_container = AsyncMock()
    manager.stop_wa_container = AsyncMock()
    manager.switch_wa_network = AsyncMock()
    counter = {"n": 0}

    async def run_wa_container(**kwargs):
        counter["n"] += 1
        return ContainerInfo(
            container_id=f"cid{counter['n']}",
            container_name=f"cr-wa-code-{counter['n']}",
            container_ip=f"172.30.0.{10 + counter['n']}",
            sandbox_type=kwargs["sandbox_type"],
            network=kwargs["network"],
        )

    manager.run_wa_container = AsyncMock(side_effect=run_wa_container)
    sidecar = FakeSidecar(tmp_path)
    minio = FakeMinio()
    service = WorkAreaService(manager, sidecar, inputs=InputsCache(tmp_path, client_factory=lambda: minio))
    service.selftest.result = _ok_caps()
    service.netguard.apply = AsyncMock()
    service.activity = AsyncMock(return_value={"known": True, "idle": True})
    return service, manager, sidecar, minio


def _spec(wa="wa1", user="u1", role="r1", **kw) -> AcquireSpec:
    return AcquireSpec(work_area_id=wa, user_id=user, role_id=role, **kw)


def _input(minio: FakeMinio, path: str, data: bytes) -> InputFileSpec:
    spec = InputFileSpec(path, "bkt", f"userspace/u1/{path}", hashlib.sha256(data).hexdigest(), len(data))
    minio.objects[("bkt", spec.key)] = data
    return spec


# ── 分配 ────────────────────────────────────────────────────────────────────


async def test_acquire_creates_instance_with_adr_layout(env):
    service, manager, sidecar, minio = env
    spec = _spec(mounts=[SKILLS, NOTES], inputs=[_input(minio, "a.txt", b"A")], context={"README.md": "hi"})
    instance, reused = await service.acquire(spec)
    assert reused is False and instance.status == "ready" and instance.mounts_held
    kwargs = manager.run_wa_container.await_args.kwargs
    binds = {b.target: b for b in kwargs["binds"]}
    assert set(binds) == {
        "/workspace",
        "/workspace/work", "/workspace/deliverables", "/workspace/inputs", "/workspace/context",
        "/workspace/role/skills", "/workspace/role/notes",
    }
    assert binds["/workspace"].read_only
    assert list(binds)[0] == "/workspace"
    assert not binds["/workspace/work"].read_only and not binds["/workspace/deliverables"].read_only
    assert binds["/workspace/inputs"].read_only and binds["/workspace/context"].read_only
    assert binds["/workspace/role/skills"].read_only and binds["/workspace/role/skills"].propagation == "rslave"
    assert not binds["/workspace/role/notes"].read_only
    assert binds["/workspace/work"].source == str(sidecar.root / "work-areas" / "wa1" / "work")
    assert kwargs["network"] == settings.wa_network and kwargs["api_token"] == instance.api_token
    assert json.loads(json.dumps(kwargs["mount_layout"]))[0]["name"] == "skills"
    assert sorted(sidecar.mount_calls) == sorted([SKILLS.key, NOTES.key])
    inst_dir = sidecar.root / "instances" / instance.sandbox_id
    assert (inst_dir / "inputs" / "a.txt").read_bytes() == b"A"
    assert (inst_dir / "context" / "README.md").read_text() == "hi"
    report = service.effective_report(instance)
    assert report["cwd"] == "/workspace/work" and report["inputs"] == {"files": 1}
    assert report["isolation"]["privileged"] is False and report["isolation"]["storage_credentials"] is False


async def test_acquire_deny_mode_uses_isolated_network(env):
    service, manager, _, _ = env
    await service.acquire(_spec(network_mode="deny"))
    assert manager.run_wa_container.await_args.kwargs["network"] == settings.wa_network_isolated


async def test_reuse_same_shape_updates_views_in_place(env):
    service, manager, sidecar, minio = env
    first, _ = await service.acquire(_spec(mounts=[SKILLS], inputs=[_input(minio, "a.txt", b"A")]))
    second, reused = await service.acquire(
        _spec(mounts=[SKILLS], inputs=[_input(minio, "b.txt", b"B")], session_id="s2", turn_id="t2")
    )
    assert reused and second is first and manager.run_wa_container.await_count == 1
    view = sidecar.root / "instances" / first.sandbox_id / "inputs"
    assert sorted(p.name for p in view.iterdir()) == ["b.txt"]
    assert first.session_id == "s2" and first.turn_id == "t2"
    assert service.mounts.refs() == {SKILLS.key: 1}  # 复用不重复取引用


async def test_reuse_keeps_views_when_inputs_omitted(env):
    service, _, sidecar, minio = env
    first, _ = await service.acquire(_spec(inputs=[_input(minio, "a.txt", b"A")]))
    await service.acquire(_spec(inputs=None))
    assert (sidecar.root / "instances" / first.sandbox_id / "inputs" / "a.txt").exists()


async def test_shape_change_rebuilds_when_idle(env):
    service, manager, sidecar, _ = env
    first, _ = await service.acquire(_spec(mounts=[SKILLS]))
    second, reused = await service.acquire(_spec(mounts=[SKILLS, NOTES]))
    assert not reused and second.sandbox_id != first.sandbox_id
    manager.remove_container.assert_any_await(first.container_info.container_id)
    assert service.mounts.refs() == {SKILLS.key: 1, NOTES.key: 1}
    assert not (sidecar.root / "instances" / first.sandbox_id).exists()
    assert (sidecar.root / "work-areas" / "wa1" / "work").is_dir()  # 工作区保留


async def test_shape_change_busy_returns_409_and_keeps_instance(env):
    service, manager, _, _ = env
    first, _ = await service.acquire(_spec(sandbox_type="code"))
    service.activity.return_value = {"known": True, "idle": False, "running_jobs": 1}
    with pytest.raises(WorkAreaError) as ei:
        await service.acquire(_spec(sandbox_type="ubuntu"))
    assert ei.value.status == 409 and ei.value.reason == "instance_busy"
    assert ei.value.extra["holders"][0]["sandbox_id"] == first.sandbox_id
    assert ei.value.extra["holders"][0]["activity"]["running_jobs"] == 1
    assert service.registry.get(first.sandbox_id) is first
    manager.remove_container.assert_not_awaited()


async def test_unhealthy_instance_is_rebuilt(env):
    service, manager, _, _ = env
    first, _ = await service.acquire(_spec())
    manager.is_healthy.return_value = False
    second, reused = await service.acquire(_spec())
    assert not reused and second.sandbox_id != first.sandbox_id


async def test_dead_mount_on_reuse_rebuilds(env):
    service, manager, sidecar, _ = env
    first, _ = await service.acquire(_spec(mounts=[SKILLS]))
    sidecar.mounted[SKILLS.key] = False  # rclone 退出
    second, reused = await service.acquire(_spec(mounts=[SKILLS]))
    assert not reused and second.sandbox_id != first.sandbox_id
    assert sidecar.unmount_calls == [SKILLS.key] and sidecar.mount_calls.count(SKILLS.key) == 2


async def test_inputs_failure_on_reuse_keeps_instance(env):
    service, manager, _, minio = env
    first, _ = await service.acquire(_spec())
    bad = InputFileSpec("x.txt", "bkt", "missing", "b" * 64, 3)
    with pytest.raises(WorkAreaError) as ei:
        await service.acquire(_spec(inputs=[bad]))
    assert ei.value.status == 422 and ei.value.reason == "inputs_unavailable"
    assert service.registry.get(first.sandbox_id) is first
    manager.remove_container.assert_not_awaited()


async def test_owner_mismatch_is_rejected(env):
    service, _, _, _ = env
    await service.acquire(_spec(user="u1"))
    with pytest.raises(WorkAreaError) as ei:
        await service.acquire(_spec(user="u2"))
    assert ei.value.status == 409 and ei.value.reason == "work_area_owner_mismatch"


async def test_limits_return_holders(env):
    service, _, _, _ = env
    await service.acquire(_spec(wa="wa1"))
    await service.acquire(_spec(wa="wa2"))
    with pytest.raises(WorkAreaError) as ei:
        await service.acquire(_spec(wa="wa3"))
    assert ei.value.status == 409 and ei.value.reason == "role_limit"
    assert {h["work_area_id"] for h in ei.value.extra["holders"]} == {"wa1", "wa2"}
    await service.acquire(_spec(wa="wa4", role="r2"))
    await service.acquire(_spec(wa="wa5", role="r3"))
    with pytest.raises(WorkAreaError) as ei:
        await service.acquire(_spec(wa="wa6", role="r4"))
    assert ei.value.reason == "user_limit" and ei.value.extra["limit"] == 4


async def test_selftest_failure_refuses_without_fallback(env):
    service, manager, _, _ = env
    service.selftest.result.items["isolation_storage"] = Capability(False, "minio reachable")
    with pytest.raises(WorkAreaError) as ei:
        await service.acquire(_spec())
    assert ei.value.status == 503 and ei.value.reason == "isolation_unavailable"
    assert "isolation_storage" in ei.value.extra["failing"]
    manager.run_wa_container.assert_not_awaited()


async def test_selftest_pending_refuses(env):
    service, _, _, _ = env
    service.selftest.result = Capabilities()
    with pytest.raises(WorkAreaError) as ei:
        await service.acquire(_spec())
    assert ei.value.extra["failing"] == ["selftest_pending"]


def test_capabilities_add_procs_and_engines_without_changing_existing_fields(env):
    service, _, _, _ = env
    caps = service.selftest.result
    before = service.capabilities()
    caps.images = {"code": ImageFeatures(procs=True, engines={"pi": "1.4.0", "dsh": "0.2.1-rc.3"})}
    data = service.capabilities()
    assert data["procs"] == {"ubuntu": False, "code": True}
    assert data["engines"] == {"ubuntu": {}, "code": {"pi": "1.4.0", "dsh": "0.2.1-rc.3"}}
    unchanged = {k: v for k, v in data.items() if k not in ("procs", "engines")}
    assert unchanged == {k: v for k, v in before.items() if k not in ("procs", "engines")}
    for key in ("ok", "checked_at", "failing", "items", "sandbox_types", "layout_version", "cwd", "limits", "instances"):
        assert key in data


async def test_create_failure_rolls_back(env):
    service, manager, sidecar, _ = env
    manager.run_wa_container.side_effect = RuntimeError("boom")
    with pytest.raises(WorkAreaError) as ei:
        await service.acquire(_spec(mounts=[SKILLS]))
    assert ei.value.status == 503 and ei.value.reason == "instance_start_failed"
    assert service.registry.list_all() == []
    assert service.mounts.refs() == {}
    assert list((sidecar.root / "instances").iterdir()) == []


# ── 释放 / 删除 ─────────────────────────────────────────────────────────────


async def test_release_destroys_container_and_keeps_work_area(env):
    service, manager, sidecar, _ = env
    instance, _ = await service.acquire(_spec(mounts=[SKILLS]))
    (sidecar.root / "work-areas" / "wa1" / "work" / "keep.txt").write_text("x")
    await service.release(instance.sandbox_id)
    manager.remove_container.assert_awaited_with(instance.container_info.container_id)
    assert service.registry.list_all() == [] and service.mounts.refs() == {}
    assert (sidecar.root / "work-areas" / "wa1" / "work" / "keep.txt").exists()
    with pytest.raises(WorkAreaError) as ei:
        await service.release(instance.sandbox_id)
    assert ei.value.status == 404


async def test_shared_mount_refcount_and_grace_sweep(env):
    service, _, sidecar, _ = env
    a, _ = await service.acquire(_spec(wa="wa1", mounts=[SKILLS]))
    b, _ = await service.acquire(_spec(wa="wa2", mounts=[SKILLS]))
    assert sidecar.mount_calls == [SKILLS.key] and service.mounts.refs() == {SKILLS.key: 2}
    await service.release(a.sandbox_id)
    assert await service.mounts.sweep(0) == []  # 仍被 b 引用
    await service.release(b.sandbox_id)
    assert await service.mounts.sweep(3600) == []  # 宽限期内不卸
    assert await service.mounts.sweep(0) == [SKILLS.key]


async def test_delete_work_area(env):
    service, manager, sidecar, _ = env
    instance, _ = await service.acquire(_spec())
    with pytest.raises(WorkAreaError) as ei:
        await service.delete_work_area("wa1")
    assert ei.value.reason == "work_area_in_use"
    assert await service.delete_work_area("wa1", force=True) == {"removed": True}
    sidecar.fsops.assert_awaited_with("rmwa", "wa1")
    assert service.registry.list_all() == []


async def test_update_inputs_and_context(env):
    service, _, sidecar, minio = env
    instance, _ = await service.acquire(_spec())
    result = await service.update_inputs(instance.sandbox_id, [_input(minio, "n.txt", b"N")])
    assert result["files"] == 1
    assert await service.update_context(instance.sandbox_id, {"a.md": "x"}) == ["a.md"]
    with pytest.raises(WorkAreaError) as ei:
        await service.update_context(instance.sandbox_id, {"../x": "y"})
    assert ei.value.reason == "invalid_context"


async def test_apply_network_all_switches_matching_instances(env):
    service, manager, _, _ = env
    mine, _ = await service.acquire(_spec("wa1"))
    other, _ = await service.acquire(_spec("wa2", role="r2"))
    already, _ = await service.acquire(_spec("wa3", network_mode="deny"))
    manager.switch_wa_network.reset_mock()

    switched, failed = await service.apply_network_all("deny", role_id="r1")

    assert [s["sandbox_id"] for s in switched] == [mine.sandbox_id]
    assert failed == [] and mine.network_mode == "deny" and other.network_mode == "allow"
    assert manager.switch_wa_network.await_count == 1
    assert already.network_mode == "deny"

    manager.switch_wa_network.side_effect = RuntimeError("boom")
    switched, failed = await service.apply_network_all("allow")
    assert switched == []
    assert {f["sandbox_id"] for f in failed} == {mine.sandbox_id, already.sandbox_id}
    assert service.registry.get(mine.sandbox_id) is mine  # 切网失败不销毁实例


async def test_usage_reports_soft_cap(env, monkeypatch):
    service, _, sidecar, _ = env
    monkeypatch.setattr(settings, "WORK_AREA_SOFT_CAP_BYTES", 10)
    sidecar.fsops.return_value = {"exists": True, "bytes": 12, "files": 1}
    usage = await service.work_areas.usage("wa1", fresh=True)
    assert usage["soft_cap_bytes"] == 10 and usage["over_soft_cap"] is True
    monkeypatch.setattr(settings, "WORK_AREA_SOFT_CAP_BYTES", 0)
    usage = await service.work_areas.usage("wa1")
    assert usage["cached"] is True and usage["over_soft_cap"] is False


# ── 重启收养 / 对账 ─────────────────────────────────────────────────────────


def _managed(sid: str, wa: str, *, mounts=(SKILLS,), status="running", token="tok", age=3600) -> ManagedContainer:
    layout = [{"name": m.name, "bucket": m.bucket, "prefix": m.prefix, "mode": m.mode} for m in mounts]
    return ManagedContainer(
        container_id=f"c-{sid}",
        container_name=f"cr-wa-code-{sid}",
        status=status,
        sandbox_type="code",
        mounted=False,
        created_at=datetime.now(tz=timezone.utc) - timedelta(seconds=age),
        container_ip="172.30.0.50" if status == "running" else "",
        network=settings.wa_network,
        labels={
            V2_LABEL: "true", SANDBOX_ID_LABEL: sid, WORK_AREA_LABEL: wa, USER_LABEL: "u1", ROLE_LABEL: "r1",
            MOUNT_LAYOUT_LABEL: json.dumps(layout),
        },
        api_token=token,
    )


async def test_startup_adopts_live_instances_and_restores_refs(env):
    service, manager, sidecar, _ = env
    sidecar.mounted[SKILLS.key] = True
    (sidecar.root / "instances" / "sb_stale").mkdir(parents=True)
    manager.list_managed.return_value = [
        _managed("sb_a", "wa1"),
        _managed("sb_b", "wa2", token=""),  # 缺 token：不能代理，清理
        _managed("sb_c", "wa3", status="exited"),
    ]
    await service.startup()
    assert service.started
    assert [r.sandbox_id for r in service.registry.list_all()] == ["sb_a"]
    adopted = service.registry.get("sb_a")
    assert adopted.mounts_held and adopted.api_token == "tok" and adopted.mounts == (SKILLS,)
    assert service.mounts.refs() == {SKILLS.key: 1}
    removed = {c.args[0] for c in manager.remove_container.await_args_list}
    assert removed == {"c-sb_b", "c-sb_c"}
    assert not (sidecar.root / "instances" / "sb_stale").exists()


async def test_startup_drops_instances_when_mounts_lost(env):
    service, manager, sidecar, _ = env
    sidecar.ensure.return_value = True  # cr-fs 刚重建
    manager.list_managed.return_value = [_managed("sb_a", "wa1")]
    await service.startup()
    assert service.registry.list_all() == []
    manager.remove_container.assert_awaited_with("c-sb_a")


async def test_startup_failure_keeps_v2_disabled(env):
    service, manager, _, _ = env
    manager.ensure_wa_networks.side_effect = RuntimeError("icc enabled")
    await service.startup()
    assert service.started is False


async def test_reconcile_discards_gone_ttl_and_orphans(env, monkeypatch):
    service, manager, sidecar, _ = env
    service.run_selftest = AsyncMock()
    await service.startup()
    gone, _ = await service.acquire(_spec(wa="wa1"))
    stale, _ = await service.acquire(_spec(wa="wa2"))
    stale.last_active_at = datetime.now(tz=timezone.utc) - timedelta(days=2)
    monkeypatch.setattr(settings, "WORK_AREA_IDLE_BACKSTOP_TTL", 86400)
    live = [
        _managed(stale.sandbox_id, "wa2", mounts=()),
        _managed("sb_orphan", "wa9", mounts=()),
        _managed("sb_young", "wa8", mounts=(), age=5),
        _managed("sb_dead", "wa7", mounts=(), status="exited"),
    ]
    live[0].container_id = stale.container_info.container_id
    manager.list_managed.return_value = live
    manager.remove_container.reset_mock()
    await service.reconcile_once()
    assert service.registry.list_all() == []  # gone：容器不在；stale：超过兜底 TTL
    removed = {c.args[0] for c in manager.remove_container.await_args_list}
    assert {"c-sb_orphan", "c-sb_dead", stale.container_info.container_id} <= removed
    assert "c-sb_young" not in removed  # 孤儿宽限期内不动


async def test_reconcile_fs_restart_discards_everything(env):
    service, manager, sidecar, _ = env
    service.run_selftest = AsyncMock()
    await service.startup()
    instance, _ = await service.acquire(_spec(mounts=[SKILLS]))
    sidecar.ensure.return_value = True
    manager.list_managed.return_value = []
    await service.reconcile_once()
    assert service.registry.list_all() == [] and service.mounts.refs() == {}


async def test_fs_errors_surface_from_reads(env):
    service, _, sidecar, _ = env
    sidecar.fsops.side_effect = FsOpError(3, "missing")
    with pytest.raises(FsOpError) as ei:
        await service.work_areas.usage("wa1", fresh=True)
    assert ei.value.not_found


async def test_quiesce_is_idempotent_preserves_work_area_and_allows_release(env):
    service, manager, sidecar, _ = env
    instance, _ = await service.acquire(_spec())
    await service.quiesce(instance.sandbox_id)
    await service.quiesce(instance.sandbox_id)
    assert instance.status == "quiesced"
    with pytest.raises(WorkAreaError): service.get(instance.sandbox_id)
    await service.release(instance.sandbox_id)
    assert service.registry.list_all() == []
    manager.remove_container.assert_awaited_once()
    assert service.work_areas.host_dir(instance.work_area_id).exists()


async def test_acquire_rebuilds_quiesced_instance(env):
    service, manager, _, _ = env
    one, _ = await service.acquire(_spec())
    await service.quiesce(one.sandbox_id)
    two, reused = await service.acquire(_spec())
    assert not reused and two.sandbox_id != one.sandbox_id


async def test_reused_instance_restores_latest_execution_origin(env):
    service, _, _, _ = env
    instance, _ = await service.acquire(_spec(session_id='s-old', turn_id='t-old'))
    await service.acquire(_spec(session_id='s-later', turn_id='t-later'))
    instance.session_id, instance.turn_id = 's-old', 't-old'
    service._restore_origin(instance)
    assert (instance.session_id, instance.turn_id) == ('s-later', 't-later')
    assert (service.dirs.path(instance.sandbox_id)/'origin.json').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('failure', ['missing', 'corrupt', 'foreign-owner'])
async def test_unknown_execution_origin_does_not_guess_from_old_labels(env, failure):
    service, _, _, _ = env
    instance, _ = await service.acquire(_spec(session_id='s-old', turn_id='t-old'))
    path = service.dirs.path(instance.sandbox_id)/'origin.json'
    if failure == 'missing':
        path.unlink()
    elif failure == 'corrupt':
        path.write_text('{')
    else:
        value = json.loads(path.read_text())
        value['user_id'] = 'other'
        path.write_text(json.dumps(value))
    service._restore_origin(instance)
    assert instance.session_id == instance.turn_id == ''
