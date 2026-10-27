"""角色目录挂载并发、共享引用与失败收尾。"""

import asyncio
from collections import Counter

import pytest

from src.manager.fs_sidecar import RoleMounts
from src.models import RoleMountSpec


def _spec(name: str) -> RoleMountSpec:
    return RoleMountSpec(name, "bucket", f"roles/r1/{name}", "rw")


class FakeSidecar:
    def __init__(self) -> None:
        self.mounted: dict[str, bool] = {}
        self.calls: list[str] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.expected_started = 1

    async def mount_status(self) -> dict[str, bool]:
        return dict(self.mounted)

    async def mount(self, spec: RoleMountSpec) -> None:
        self.calls.append(spec.key)
        if len(self.calls) >= self.expected_started:
            self.started.set()
        await self.release.wait()
        self.mounted[spec.key] = True

    async def unmount(self, key: str) -> None:
        self.mounted.pop(key, None)


async def test_independent_mounts_start_in_parallel() -> None:
    sidecar = FakeSidecar()
    sidecar.expected_started = 4
    mounts = RoleMounts(sidecar)
    specs = [_spec(name) for name in ("notes", "skills", "knowledge", "diary")]
    task = asyncio.create_task(mounts.acquire(specs))
    try:
        # 都开始后才放行，保证验证的是并发，而非耗时阈值。
        await asyncio.wait_for(sidecar.started.wait(), timeout=1)
        sidecar.release.set()
        await task
        assert mounts.refs() == {spec.key: 1 for spec in specs}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_overlapping_acquires_share_one_mount() -> None:
    sidecar = FakeSidecar()
    mounts = RoleMounts(sidecar)
    spec = _spec("notes")
    first = asyncio.create_task(mounts.acquire([spec]))
    await sidecar.started.wait()
    second = asyncio.create_task(mounts.acquire([spec]))
    await asyncio.sleep(0)
    sidecar.release.set()
    await asyncio.gather(first, second)
    assert Counter(sidecar.calls) == {spec.key: 1}
    assert mounts.refs() == {spec.key: 2}


async def test_mount_concurrency_is_shared_across_acquires() -> None:
    sidecar = FakeSidecar()
    sidecar.expected_started = 4
    mounts = RoleMounts(sidecar)
    specs = [_spec(f"dir{index}") for index in range(8)]
    first = asyncio.create_task(mounts.acquire(specs[:4]))
    second = asyncio.create_task(mounts.acquire(specs[4:]))
    try:
        await asyncio.wait_for(sidecar.started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert len(sidecar.calls) == 4
        sidecar.release.set()
        await asyncio.gather(first, second)
        assert len(sidecar.calls) == 8
    finally:
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


async def test_cancellation_releases_already_acquired_references() -> None:
    done = _spec("notes")
    blocked = _spec("skills")
    waiting = asyncio.Event()
    finish_mount = asyncio.Event()

    class CancelSidecar(FakeSidecar):
        async def mount(self, spec: RoleMountSpec) -> None:
            if spec == blocked:
                waiting.set()
                await finish_mount.wait()
            self.mounted[spec.key] = True

    mounts = RoleMounts(CancelSidecar())
    task = asyncio.create_task(mounts.acquire([done, blocked]))
    await asyncio.wait_for(waiting.wait(), timeout=1)
    assert mounts.refs() == {done.key: 1}
    task.cancel()
    finish_mount.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert mounts.refs() == {}


async def test_cancelled_inflight_mount_keeps_key_locked_until_mount_finishes() -> None:
    spec = _spec("skills")
    sidecar = FakeSidecar()
    mounts = RoleMounts(sidecar)
    first = asyncio.create_task(mounts.acquire([spec]))
    await asyncio.wait_for(sidecar.started.wait(), timeout=1)
    first.cancel()
    second = asyncio.create_task(mounts.acquire([spec]))
    try:
        await asyncio.sleep(0.05)
        assert sidecar.calls == [spec.key]  # 等在途挂载结束，不能再次创建同一挂载
        sidecar.release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        assert sidecar.calls == [spec.key]
        assert mounts.refs() == {spec.key: 1}
    finally:
        sidecar.release.set()
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


async def test_failure_releases_other_acquired_references() -> None:
    done = _spec("notes")
    failing = _spec("skills")
    completed = asyncio.Event()

    class FailingSidecar(FakeSidecar):
        async def mount(self, spec: RoleMountSpec) -> None:
            if spec == failing:
                await completed.wait()
                raise RuntimeError("mount failed")
            self.mounted[spec.key] = True
            completed.set()

    mounts = RoleMounts(FailingSidecar())
    with pytest.raises(RuntimeError, match="mount failed"):
        await mounts.acquire([done, failing])
    assert mounts.refs() == {}
