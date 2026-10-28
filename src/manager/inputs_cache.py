"""
inputs 视图：用户 blob 本地缓存 + 每实例硬链接目录（ADR 0025）

平台按授权清单给出实例可见的用户文件（MinIO 对象 + sha256）。Hub 把 blob 下载到
``inputs-cache/<user_id>/<sha256>``（校验哈希与大小、0444），再在
``instances/<sandbox_id>/inputs/<path>`` 建硬链接组成只读视图。授权变化时就地增删链接，
不替换目录（实例里的 bind 指向目录 inode）。

硬链接要求 Hub 进程拥有 blob（``fs.protected_hardlinks``），所以下载由 Hub 自己完成，
不经 cr-fs。缓存超过上限时按最近使用时间淘汰未被任何视图引用（``st_nlink == 1``）的 blob。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Callable, Iterable, Optional

from loguru import logger

from src.config import settings
from src.manager.paths import require_id, split_relative
from src.models import InputFileSpec

_CHUNK = 1024 * 1024
_SHA_HEX = frozenset("0123456789abcdef")
MANIFEST = "inputs.json"


class InputsError(RuntimeError):
    pass


def _minio_client():
    from minio import Minio

    return Minio(
        settings.MINIO_ENDPOINT,
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY,
        secure=settings.MINIO_SECURE,
    )


def validate_inputs(files: Iterable[InputFileSpec]) -> list[InputFileSpec]:
    seen: set[str] = set()
    out: list[InputFileSpec] = []
    for spec in files:
        split_relative(spec.path)
        if spec.path in seen:
            raise ValueError(f"inputs 路径重复：{spec.path}")
        if len(spec.sha256) != 64 or not set(spec.sha256) <= _SHA_HEX:
            raise ValueError(f"inputs sha256 非法：{spec.path}")
        if spec.size < 0 or not spec.bucket or not spec.key:
            raise ValueError(f"inputs 条目不完整：{spec.path}")
        seen.add(spec.path)
        out.append(spec)
    # 一个路径不能既是文件又是另一个文件的父目录
    for path in seen:
        parts = path.split("/")
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in seen:
                raise ValueError(f"inputs 路径冲突：{path}")
    return out


class InputsCache:
    def __init__(self, root: Path, client_factory: Optional[Callable] = None) -> None:
        self._root = root / "inputs-cache"
        self._client_factory = client_factory or _minio_client
        self._client = None
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _get_client(self):
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def blob_path(self, user_id: str, sha256: str) -> Path:
        require_id(user_id, "用户 ID")
        return self._root / user_id / sha256

    # ── blob ────────────────────────────────────────────────────────────────

    def _download_sync(self, spec: InputFileSpec, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{spec.sha256}.{uuid.uuid4().hex}.part")
        hasher = hashlib.sha256()
        size = 0
        response = self._get_client().get_object(spec.bucket, spec.key)
        try:
            with open(tmp, "wb") as fh:
                for chunk in response.stream(_CHUNK):
                    fh.write(chunk)
                    hasher.update(chunk)
                    size += len(chunk)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            response.close()
            response.release_conn()
        if hasher.hexdigest() != spec.sha256 or size != spec.size:
            tmp.unlink(missing_ok=True)
            raise InputsError(
                f"blob 校验失败 | key={spec.key} | 期望 {spec.sha256}/{spec.size} 实得 {hasher.hexdigest()}/{size}"
            )
        os.chmod(tmp, 0o444)
        os.replace(tmp, dest)

    async def ensure_blob(self, user_id: str, spec: InputFileSpec) -> Path:
        path = self.blob_path(user_id, spec.sha256)
        if self._usable(path, spec.size):
            self._touch(path)
            return path
        async with self._locks[f"{user_id}/{spec.sha256}"]:
            if not self._usable(path, spec.size):
                try:
                    await asyncio.to_thread(self._download_sync, spec, path)
                except InputsError:
                    raise
                except Exception as exc:
                    raise InputsError(f"blob 下载失败 | key={spec.key} | err={exc}") from exc
                logger.info(f"inputs blob 已缓存 | user={user_id} | sha256={spec.sha256[:12]} | size={spec.size}")
        return path

    @staticmethod
    def _touch(path: Path) -> None:
        """刷新最近使用时间，避免刚确认可用的 blob 在建链接前被淘汰。"""
        try:
            os.utime(path)
        except OSError:
            pass

    @staticmethod
    def _usable(path: Path, size: int) -> bool:
        try:
            return path.stat().st_size == size
        except FileNotFoundError:
            return False

    # ── 视图 ────────────────────────────────────────────────────────────────

    async def materialize(
        self, instance_dir: Path, user_id: str, files: Iterable[InputFileSpec]
    ) -> dict:
        """把实例的 inputs 视图同步为 ``files``；先备齐 blob，再就地增删硬链接。"""
        specs = validate_inputs(files)
        blobs = {spec.path: await self.ensure_blob(user_id, spec) for spec in specs}
        return await asyncio.to_thread(self._sync_view, instance_dir, specs, blobs)

    def _sync_view(self, instance_dir: Path, specs: list[InputFileSpec], blobs: dict[str, Path]) -> dict:
        view = instance_dir / "inputs"
        view.mkdir(parents=True, exist_ok=True, mode=0o755)
        manifest_path = instance_dir / MANIFEST
        wanted = {spec.path: spec for spec in specs}

        # 以目录实况为准（清单可能因崩溃缺失）：不在清单里、或指向的 blob 变了的链接都删掉。
        for entry in [p for p in view.rglob("*") if not p.is_dir() or p.is_symlink()]:
            rel = entry.relative_to(view).as_posix()
            blob = blobs.get(rel)
            try:
                keep = blob is not None and os.path.samestat(entry.lstat(), blob.stat())
            except FileNotFoundError:
                keep = False
            if not keep:
                entry.unlink(missing_ok=True)
        self._prune_empty_dirs(view)

        for spec in specs:
            target = view / spec.path
            blob = blobs[spec.path]
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.link")
            try:
                os.link(blob, tmp)
            except FileNotFoundError as exc:
                raise InputsError(f"blob 已被淘汰，请重试 | path={spec.path}") from exc
            os.replace(tmp, target)
            self._touch(blob)

        tmp_manifest = manifest_path.with_suffix(".json.tmp")
        tmp_manifest.write_text(json.dumps({s.path: s.sha256 for s in specs}, ensure_ascii=False))
        os.replace(tmp_manifest, manifest_path)
        return {"files": len(specs), "bytes": sum(s.size for s in specs)}

    @staticmethod
    def _prune_empty_dirs(view: Path) -> None:
        for path in sorted((p for p in view.rglob("*") if p.is_dir()), reverse=True):
            try:
                path.rmdir()
            except OSError:
                pass

    # ── 淘汰 ────────────────────────────────────────────────────────────────

    def _evict_sync(self, max_bytes: int) -> int:
        if max_bytes <= 0 or not self._root.is_dir():
            return 0
        blobs: list[tuple[float, int, Path, int]] = []
        total = 0
        for user_dir in self._root.iterdir():
            if not user_dir.is_dir():
                continue
            for blob in user_dir.iterdir():
                try:
                    st = blob.stat()
                except FileNotFoundError:
                    continue
                total += st.st_size
                blobs.append((st.st_mtime, st.st_size, blob, st.st_nlink))
        removed = 0
        for _, size, blob, nlink in sorted(blobs):
            if total <= max_bytes:
                break
            if nlink > 1 or blob.name.endswith(".part"):
                continue
            blob.unlink(missing_ok=True)
            total -= size
            removed += 1
        return removed

    async def evict(self, max_bytes: Optional[int] = None) -> int:
        limit = settings.INPUTS_CACHE_MAX_BYTES if max_bytes is None else max_bytes
        removed = await asyncio.to_thread(self._evict_sync, limit)
        if removed:
            logger.info(f"inputs blob 缓存淘汰 | removed={removed}")
        return removed

    async def prune_v1_views(self, mounted: set[str], grace_seconds: float = 60) -> int:
        """Release hard links of views no longer mounted by any live container."""
        def run():
            views = self._root.parent / "v1-inputs"
            if not views.is_dir():
                return 0
            protected = {str(Path(path).resolve()) for path in mounted}
            removed = 0
            for view in views.iterdir():
                if not view.is_dir() or view.is_symlink():
                    continue
                if str((view / "inputs").resolve()) in protected or time.time() - view.stat().st_mtime < grace_seconds:
                    continue
                shutil.rmtree(view)
                removed += 1
            return removed
        removed = await asyncio.to_thread(run)
        await self.evict()
        return removed
