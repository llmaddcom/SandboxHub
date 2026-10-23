"""inputs 硬链接视图与 context 目录（宿主侧，Hub 进程直接操作）。MinIO 客户端用假实现。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from src.manager.inputs_cache import InputsCache, InputsError
from src.manager.work_areas import InstanceDirs
from src.models import InputFileSpec


class _Response:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def stream(self, chunk: int):
        for i in range(0, len(self._data), chunk):
            yield self._data[i : i + chunk]

    def close(self) -> None:
        pass

    def release_conn(self) -> None:
        pass


class FakeMinio:
    def __init__(self, objects: dict[tuple[str, str], bytes]) -> None:
        self.objects = objects
        self.calls: list[tuple[str, str]] = []

    def get_object(self, bucket: str, key: str):
        self.calls.append((bucket, key))
        if (bucket, key) not in self.objects:
            raise RuntimeError(f"NoSuchKey {bucket}/{key}")
        return _Response(self.objects[(bucket, key)])


def _spec(path: str, data: bytes, key: str | None = None) -> InputFileSpec:
    return InputFileSpec(
        path=path, bucket="b", key=key or f"userspace/u1/{path}", sha256=hashlib.sha256(data).hexdigest(), size=len(data)
    )


@pytest.fixture
def cache_and_client(tmp_path):
    client = FakeMinio({})
    cache = InputsCache(tmp_path, client_factory=lambda: client)
    return cache, client, tmp_path


async def test_materialize_links_blobs_and_writes_manifest(cache_and_client):
    cache, client, root = cache_and_client
    a, b = b"alpha", b"beta-data"
    specs = [_spec("a.txt", a), _spec("docs/b.md", b)]
    for s, data in zip(specs, (a, b)):
        client.objects[(s.bucket, s.key)] = data
    inst = root / "instances" / "sb_1"
    result = await cache.materialize(inst, "u1", specs)
    assert result == {"files": 2, "bytes": len(a) + len(b)}
    view = inst / "inputs"
    assert (view / "a.txt").read_bytes() == a and (view / "docs" / "b.md").read_bytes() == b
    blob = cache.blob_path("u1", specs[0].sha256)
    assert os.path.samestat((view / "a.txt").stat(), blob.stat())  # 硬链接，不复制
    assert oct(blob.stat().st_mode & 0o777) == "0o444"
    assert json.loads((inst / "inputs.json").read_text()) == {s.path: s.sha256 for s in specs}

    # 第二次：blob 已缓存，不再下载；视图收缩时删除多余链接与空目录
    client.calls.clear()
    await cache.materialize(inst, "u1", specs[:1])
    assert client.calls == []
    assert not (view / "docs").exists() and (view / "a.txt").exists()


async def test_materialize_replaces_changed_version(cache_and_client):
    cache, client, root = cache_and_client
    v1, v2 = b"version-1", b"version-2!"
    s1, s2 = _spec("f.txt", v1, key="k1"), _spec("f.txt", v2, key="k2")
    client.objects[("b", "k1")] = v1
    client.objects[("b", "k2")] = v2
    inst = root / "instances" / "sb_1"
    await cache.materialize(inst, "u1", [s1])
    view_dir = inst / "inputs"
    inode_before = os.stat(view_dir).st_ino
    await cache.materialize(inst, "u1", [s2])
    assert (view_dir / "f.txt").read_bytes() == v2
    assert os.stat(view_dir).st_ino == inode_before  # 目录不换 inode（实例 bind 指向它）


async def test_hash_mismatch_and_download_errors_raise_inputs_error(cache_and_client):
    cache, client, root = cache_and_client
    spec = _spec("x.txt", b"expected")
    client.objects[(spec.bucket, spec.key)] = b"tampered"
    with pytest.raises(InputsError, match="校验失败"):
        await cache.materialize(root / "instances" / "sb_1", "u1", [spec])
    assert not cache.blob_path("u1", spec.sha256).exists()
    assert not list(cache.blob_path("u1", spec.sha256).parent.glob("*.part"))

    missing = _spec("y.txt", b"y", key="absent")
    with pytest.raises(InputsError, match="下载失败"):
        await cache.materialize(root / "instances" / "sb_1", "u1", [missing])


async def test_blobs_are_per_user(cache_and_client):
    cache, client, root = cache_and_client
    data = b"same bytes"
    spec = _spec("x", data)
    client.objects[(spec.bucket, spec.key)] = data
    await cache.materialize(root / "instances" / "sb_1", "u1", [spec])
    await cache.materialize(root / "instances" / "sb_2", "u2", [spec])
    assert cache.blob_path("u1", spec.sha256) != cache.blob_path("u2", spec.sha256)
    with pytest.raises(ValueError):
        cache.blob_path("../u1", spec.sha256)


async def test_evict_keeps_linked_blobs(cache_and_client):
    cache, client, root = cache_and_client
    linked, loose = b"linked" * 100, b"loose" * 100
    s_linked, s_loose = _spec("l", linked, key="kl"), _spec("o", loose, key="ko")
    client.objects[("b", "kl")] = linked
    client.objects[("b", "ko")] = loose
    await cache.materialize(root / "instances" / "sb_1", "u1", [s_linked])
    await cache.ensure_blob("u1", s_loose)
    removed = await cache.evict(max_bytes=1)
    assert removed == 1
    assert cache.blob_path("u1", s_linked.sha256).exists()
    assert not cache.blob_path("u1", s_loose.sha256).exists()


# ── context ─────────────────────────────────────────────────────────────────


class _FakeSidecar:
    def __init__(self, root: Path) -> None:
        self.root = root

    def host_path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)


def test_write_context_replaces_in_place(tmp_path):
    dirs = InstanceDirs(_FakeSidecar(tmp_path))
    dirs.create("sb_1")
    ctx = dirs.context_dir("sb_1")
    inode = os.stat(ctx).st_ino
    assert dirs.write_context("sb_1", {"README.md": "a", "role/notes.md": "b"}) == ["README.md", "role/notes.md"]
    assert (ctx / "role" / "notes.md").read_text() == "b"
    dirs.write_context("sb_1", {"README.md": "updated"})
    assert (ctx / "README.md").read_text() == "updated"
    assert not (ctx / "role").exists()
    assert os.stat(ctx).st_ino == inode
    assert not [p for p in ctx.iterdir() if p.name.startswith(".ctx-")]


@pytest.mark.parametrize(
    "files",
    [
        {"../escape": "x"},
        {"/abs": "x"},
        {"a": "x", "a/b": "y"},
        {f"f{i}": "x" for i in range(65)},
        {"big": "x" * (1024 * 1024 + 1)},
    ],
)
def test_write_context_rejects(tmp_path, files):
    dirs = InstanceDirs(_FakeSidecar(tmp_path))
    dirs.create("sb_1")
    with pytest.raises(ValueError):
        dirs.write_context("sb_1", files)
    assert not (tmp_path / "escape").exists()


def test_instance_dirs_require_valid_ids(tmp_path):
    dirs = InstanceDirs(_FakeSidecar(tmp_path))
    with pytest.raises(ValueError):
        dirs.path("../x")
    dirs.create("sb_1")
    dirs.create("sb_2")
    assert sorted(dirs.list_ids()) == ["sb_1", "sb_2"]
    dirs.remove("sb_1")
    assert dirs.list_ids() == ["sb_2"]
