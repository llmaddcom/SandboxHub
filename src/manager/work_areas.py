"""
工作区与实例目录（ADR 0025）

宿主目录布局（``WORK_AREA_ROOT`` 下）：

- ``work-areas/<work_area_id>/{work,deliverables}``：工作区，实例以读写 bind 进入；
  实例回收后保留，删除工作区须显式调用。读取、统计、导入、删除经 cr-fs（root、不跟随软链接）。
- ``instances/<sandbox_id>/{inputs,context}``：每实例的只读视图，由 Hub 写入；实例释放即删。
  ``inputs.json`` 是 inputs 清单（不在 bind 范围内，租户看不到）。布局 v2 另有
  ``workspace-files/{AGENTS.md,CLAUDE.md}``：以单文件只读 bind 进 ``/workspace``，更新须就地改写
  （单文件 bind 指向 inode，rename 替换对实例不可见）。

工作区的 ``work`` / ``deliverables`` 由 Hub 直接创建（属主是 Hub 用户、0755）；租户在容器内是
root，照样可写。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import tempfile
import time
import uuid
from pathlib import Path
from typing import AsyncIterator, Optional

from loguru import logger

from src.config import settings
from src.manager.fs_sidecar import FsSidecar, ReadResult
from src.manager.layouts import WORKSPACE_FILE_MAX_BYTES, WORKSPACE_FILES
from src.manager.paths import require_id, split_relative

WORK_AREA_ROOTS = ("work", "deliverables")
CONTEXT_MAX_FILE_BYTES = 1024 * 1024
CONTEXT_MAX_FILES = 64


def _with_soft_cap(data: dict, *, cached: bool) -> dict:
    cap = settings.WORK_AREA_SOFT_CAP_BYTES
    used = int(data.get("bytes") or 0)
    return dict(data, cached=cached, soft_cap_bytes=cap, over_soft_cap=cap > 0 and used >= cap)


class WorkAreas:
    def __init__(self, sidecar: FsSidecar) -> None:
        self._fs = sidecar
        self._usage_cache: dict[str, tuple[float, dict]] = {}

    def host_dir(self, work_area_id: str) -> Path:
        require_id(work_area_id, "工作区 ID")
        return self._fs.host_path("work-areas", work_area_id)

    def bind_sources(self, work_area_id: str) -> dict[str, Path]:
        base = self.host_dir(work_area_id)
        return {root: base / root for root in WORK_AREA_ROOTS}

    def _ensure_sync(self, work_area_id: str) -> None:
        for path in self.bind_sources(work_area_id).values():
            path.mkdir(parents=True, exist_ok=True, mode=0o755)

    async def ensure(self, work_area_id: str) -> None:
        await asyncio.to_thread(self._ensure_sync, work_area_id)

    def _clear_v2_mountpoints_sync(self, work_area_id: str) -> list[str]:
        """布局 v2 把 deliverables 与平台文件叠挂在宿主 ``work/`` 里：``work/deliverables`` 须是目录、
        ``work/AGENTS.md`` 等须是普通文件或不存在。租户留下的软链接或其他类型会让挂载落到别处或
        直接失败，挪到一旁（``<名字>.moved-<时间>``），内容不删。返回被挪走的名字。"""
        work = self.bind_sources(work_area_id)["work"]
        expected = {"deliverables": stat.S_ISDIR, **{name: stat.S_ISREG for name in WORKSPACE_FILES}}
        moved: list[str] = []
        for name, ok in expected.items():
            path = work / name
            try:
                mode = os.lstat(path).st_mode
            except FileNotFoundError:
                continue
            if ok(mode):
                continue
            aside = work / f"{name}.moved-{int(time.time())}"
            try:
                os.rename(path, aside)
                moved.append(name)
                logger.warning(f"v2 挂载点被占用，已挪开 | work_area={work_area_id} | {name} -> {aside.name}")
            except OSError as exc:
                # 挪不动（租户改了属主或权限）：交给容器创建报错，原因随 instance_start_failed 返回
                logger.warning(f"v2 挂载点被占用且无法挪开 | work_area={work_area_id} | {name} | err={exc}")
        return moved

    async def clear_v2_mountpoints(self, work_area_id: str) -> list[str]:
        return await asyncio.to_thread(self._clear_v2_mountpoints_sync, work_area_id)

    def exists(self, work_area_id: str) -> bool:
        return self.host_dir(work_area_id).is_dir()

    async def usage(self, work_area_id: str, *, fresh: bool = False) -> dict:
        require_id(work_area_id, "工作区 ID")
        ttl = settings.WORK_AREA_USAGE_CACHE_SECONDS
        cached = self._usage_cache.get(work_area_id)
        now = time.monotonic()
        if cached and not fresh and now - cached[0] < ttl:
            return _with_soft_cap(cached[1], cached=True)
        data = await self._fs.fsops("usage", work_area_id)
        self._usage_cache[work_area_id] = (now, data)
        if len(self._usage_cache) > 4096:
            cutoff = now - ttl
            for key in [k for k, (at, _) in self._usage_cache.items() if at < cutoff]:
                self._usage_cache.pop(key, None)
        return _with_soft_cap(data, cached=False)

    async def list_files(self, work_area_id: str, root: str, max_files: Optional[int] = None) -> dict:
        require_id(work_area_id, "工作区 ID")
        if root not in WORK_AREA_ROOTS:
            raise ValueError(f"非法目录：{root!r}")
        limit = max(1, min(max_files or settings.WORK_AREA_LIST_MAX_FILES, settings.WORK_AREA_LIST_MAX_FILES))
        return await self._fs.fsops("list", work_area_id, root, str(limit))

    async def read(self, work_area_id: str, root: str, relpath: str) -> ReadResult:
        """把文件读到 Hub 暂存区（调用方负责删除 ``result.path``）。"""
        require_id(work_area_id, "工作区 ID")
        if root not in WORK_AREA_ROOTS:
            raise ValueError(f"非法目录：{root!r}")
        split_relative(relpath)
        staging = self._fs.host_path("staging")
        fd, tmp = tempfile.mkstemp(prefix="read-", dir=staging)
        os.close(fd)
        try:
            return await self._fs.read_to_file(work_area_id, root, relpath, Path(tmp))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    async def checksums(self, work_area_id: str, root: str, paths: list[str]) -> dict:
        """一次 cr-fs 调用核对最多 128 个文件，不把私密正文传回 Hub。"""
        require_id(work_area_id, "工作区 ID")
        if root not in WORK_AREA_ROOTS or not 1 <= len(paths) <= 128:
            raise ValueError("指纹目录或路径数量无效")
        for path in paths:
            split_relative(path)
        name = f"checksums-{uuid.uuid4().hex}.json"
        request = self._fs.host_path("staging", name)

        def write_manifest():
            fd = os.open(request, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(paths, stream, ensure_ascii=False)

        try:
            await asyncio.to_thread(write_manifest)
            return await self._fs.fsops("checksums", work_area_id, root, name)
        finally:
            request.unlink(missing_ok=True)

    async def import_tar(self, work_area_id: str, root: str, chunks: AsyncIterator[bytes]) -> dict:
        """把上传的 tar 流落到暂存区，再由 cr-fs 解包进工作区。"""
        require_id(work_area_id, "工作区 ID")
        if root not in WORK_AREA_ROOTS:
            raise ValueError(f"非法目录：{root!r}")
        await self.ensure(work_area_id)
        name = f"import-{uuid.uuid4().hex}.tar"
        host_tar = self._fs.host_path("staging", name)
        try:
            with open(host_tar, "wb") as fh:
                async for chunk in chunks:
                    fh.write(chunk)
            os.chmod(host_tar, 0o644)
            result = await self._fs.fsops(
                "import", work_area_id, root, self._fs.container_path("staging", name)
            )
        finally:
            host_tar.unlink(missing_ok=True)
        self._usage_cache.pop(work_area_id, None)
        return result

    async def remove(self, work_area_id: str) -> dict:
        require_id(work_area_id, "工作区 ID")
        result = await self._fs.fsops("rmwa", work_area_id)
        self._usage_cache.pop(work_area_id, None)
        return result


class InstanceDirs:
    """每实例的 inputs / context 只读视图目录（Hub 拥有，租户只读 bind）。"""

    def __init__(self, sidecar: FsSidecar) -> None:
        self._fs = sidecar

    def path(self, sandbox_id: str) -> Path:
        require_id(sandbox_id, "实例 ID")
        return self._fs.host_path("instances", sandbox_id)

    def inputs_dir(self, sandbox_id: str) -> Path:
        return self.path(sandbox_id) / "inputs"

    def context_dir(self, sandbox_id: str) -> Path:
        return self.path(sandbox_id) / "context"

    def root_dir(self, sandbox_id: str) -> Path:
        return self.path(sandbox_id) / "root"

    def workspace_files_dir(self, sandbox_id: str) -> Path:
        return self.path(sandbox_id) / "workspace-files"

    def workspace_file(self, sandbox_id: str, name: str) -> Path:
        if name not in WORKSPACE_FILES:
            raise ValueError(f"非法 workspace 文件：{name!r}")
        return self.workspace_files_dir(sandbox_id) / name

    def create(self, sandbox_id: str, layout_version: int = 1) -> Path:
        base = self.path(sandbox_id)
        for sub in ("inputs", "context"):
            (base / sub).mkdir(parents=True, exist_ok=True, mode=0o755)
        if layout_version >= 2:
            # v2 没有只读骨架（/workspace 就是工作区）；平台文件先建成空文件，bind 才有源
            files = self.workspace_files_dir(sandbox_id)
            files.mkdir(parents=True, exist_ok=True, mode=0o755)
            for name in WORKSPACE_FILES:
                target = files / name
                target.touch(exist_ok=True)
                target.chmod(0o644)
            return base
        root = self.root_dir(sandbox_id)
        for sub in ("inputs", "context", "work", "deliverables", "role/notes", "role/skills", "role/knowledge", "role/diary"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        for directory in sorted(root.rglob("*"), reverse=True):
            if directory.is_dir():
                directory.chmod(0o555)
        root.chmod(0o555)
        return base

    def remove(self, sandbox_id: str) -> None:
        root = self.root_dir(sandbox_id)
        if root.is_dir():
            root.chmod(0o755)
            for directory in root.rglob("*"):
                if directory.is_dir():
                    directory.chmod(0o755)
        shutil.rmtree(self.path(sandbox_id), ignore_errors=True)

    def list_ids(self) -> list[str]:
        base = self._fs.host_path("instances")
        if not base.is_dir():
            return []
        return [p.name for p in base.iterdir() if p.is_dir()]

    def write_workspace_files(self, sandbox_id: str, files: dict[str, str]) -> list[str]:
        """整组替换 ``/workspace`` 根的平台文件：给出的写入，白名单里没给的清空。

        单文件只读 bind 绑定的是 inode：只能就地截断重写，不能 rename 替换。
        """
        encoded = validate_workspace_files(files)
        written: list[str] = []
        for name in WORKSPACE_FILES:
            target = self.workspace_file(sandbox_id, name)
            data = encoded.get(name, b"")
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o644)
            with os.fdopen(fd, "wb") as fh:
                fh.truncate(0)
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            if name in encoded:
                written.append(name)
        return written

    def workspace_file_names(self, sandbox_id: str) -> list[str]:
        """内容非空的平台文件名（effective 报告用）。"""
        names = []
        for name in WORKSPACE_FILES:
            try:
                if self.workspace_file(sandbox_id, name).stat().st_size > 0:
                    names.append(name)
            except FileNotFoundError:
                continue
        return names

    def write_context(self, sandbox_id: str, files: dict[str, str]) -> list[str]:
        """就地替换 context 目录内容：写入 ``files``（相对路径 → 文本），删除其余文件。

        目录本身不换 inode（实例里的 bind 指向它），逐个文件经临时文件 + rename 原子替换。
        """
        if len(files) > CONTEXT_MAX_FILES:
            raise ValueError(f"context 文件数超过 {CONTEXT_MAX_FILES}")
        encoded: dict[str, bytes] = {}
        for rel, text in files.items():
            split_relative(rel)
            data = text.encode("utf-8")
            if len(data) > CONTEXT_MAX_FILE_BYTES:
                raise ValueError(f"context 文件过大：{rel}")
            encoded[rel] = data
        for rel in encoded:
            parts = rel.split("/")
            if any("/".join(parts[:i]) in encoded for i in range(1, len(parts))):
                raise ValueError(f"context 路径冲突：{rel}")
        base = self.context_dir(sandbox_id)
        base.mkdir(parents=True, exist_ok=True, mode=0o755)
        for rel, data in encoded.items():
            target = base / rel
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            fd, tmp = tempfile.mkstemp(prefix=".ctx-", dir=target.parent)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.chmod(tmp, 0o644)
            os.replace(tmp, target)
        wanted = {base / rel for rel in encoded}
        for path in sorted(base.rglob("*"), reverse=True):
            if path.is_file() and path not in wanted:
                path.unlink(missing_ok=True)
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        return sorted(encoded)


def validate_workspace_files(files: dict[str, str]) -> dict[str, bytes]:
    """``workspace_files`` 只收白名单名字的文本，单个不超过 ``WORKSPACE_FILE_MAX_BYTES``。"""
    encoded: dict[str, bytes] = {}
    for name, text in files.items():
        if name not in WORKSPACE_FILES:
            raise ValueError(f"workspace_files 只允许 {', '.join(WORKSPACE_FILES)}：{name!r}")
        if not isinstance(text, str):
            raise ValueError(f"workspace_files 的值须是文本：{name}")
        data = text.encode("utf-8")
        if len(data) > WORKSPACE_FILE_MAX_BYTES:
            raise ValueError(f"workspace 文件过大：{name}")
        encoded[name] = data
    return encoded
