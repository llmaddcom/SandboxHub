"""
cr-fs 特权 sidecar（ADR 0025）

租户实例不再拿任何存储能力（无 SYS_ADMIN、无 /dev/fuse、无 MinIO 凭据）；需要特权的事
全部收到这个常驻容器里：

- 角色目录挂载：每个 (bucket, prefix, mode) 一个 ``rclone mount --daemon`` 进程，挂在
  ``/srv/mnt/<key>``。cr-fs 以 rshared 方式绑定 ``WORK_AREA_ROOT`` 到 ``/srv``，挂载传播到宿主
  ``WORK_AREA_ROOT/mnt/<key>`` 后再 bind 给实例。同一挂载被多个实例共用，引用计数，归零后
  延迟卸载。``ro`` 由 rclone ``--read-only`` 与实例 bind ``ro`` 两层保证。
- 工作区文件操作：``docker exec`` 调用镜像内 fsops.py（root 身份、逐级 O_NOFOLLOW），
  因为工作区里的文件由租户以 root 写入，Hub 进程未必能读能删，也不能跟随租户软链接。
- netguard：host 网络 + 特权，代 Hub 下发宿主 iptables 规则。

不用 rclone rcd：cr-fs 在 host 网络上，rc 端口会暴露在宿主回环上，任何本机进程都能借它挂载
任意前缀；每个挂载一个进程也让单个挂载崩溃不波及其他数字人。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import docker
import docker.errors
from loguru import logger

from src.config import settings
from src.models import RoleMountSpec

FS_LABEL = "sandboxhub.fs"
FS_OWNER_LABEL = "sandboxhub.fs.owner"
CONTAINER_ROOT = "/srv"
FSOPS = ("python3", "/opt/crfs/fsops.py")
# WORK_AREA_ROOT 下的固定子目录
ROOT_SUBDIRS = ("work-areas", "instances", "inputs-cache", "mnt", "cache", "staging", "run")
# 多个实例同时启动时共享此上限，避免对 cr-fs / Docker 发起无界并发。
_ROLE_MOUNT_CONCURRENCY = 4

EXIT_INVALID = 2
EXIT_NOT_FOUND = 3
EXIT_NOT_REGULAR = 4


class FsOpError(RuntimeError):
    """fsops 非零退出：``code`` 沿用 fsops 退出码（2 非法、3 不存在、4 非普通文件）。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code

    @property
    def not_found(self) -> bool:
        return self.code == EXIT_NOT_FOUND

    @property
    def invalid(self) -> bool:
        return self.code == EXIT_INVALID

    @property
    def not_regular(self) -> bool:
        return self.code == EXIT_NOT_REGULAR


@dataclass
class ReadResult:
    """从工作区读出的一个文件（已落到 Hub 暂存文件）。"""
    path: Path
    size: int
    mtime_ns: int
    sha256: str
    stable: bool  # 读取前后 size/mtime 一致


class FsSidecar:
    """cr-fs 容器的生命周期与 exec 封装。所有 Docker 调用经 asyncio.to_thread。"""

    def __init__(self, docker_client: Optional[docker.DockerClient] = None) -> None:
        self._docker = docker_client or docker.from_env()
        self.root = settings.work_area_root
        self._ensure_lock = asyncio.Lock()
        # cr-fs 容器的启动时间：变了说明容器重建/重启过，其中的 rclone 挂载已全部丢失。
        self.generation: str = ""

    @property
    def name(self) -> str:
        return settings.fs_sidecar_name

    def host_path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    @staticmethod
    def container_path(*parts: str) -> str:
        return "/".join((CONTAINER_ROOT, *parts))

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    def _prepare_root_sync(self) -> None:
        for sub in ROOT_SUBDIRS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def _ensure_sync(self) -> tuple[str, bool]:
        """返回 (容器启动时间, 是否本次新建)。"""
        self._prepare_root_sync()
        image = settings.DOCKER_IMAGE_FS
        source = str(self.root.resolve())
        try:
            container = self._docker.containers.get(self.name)
            container.reload()
            mounts = container.attrs.get("Mounts") or []
            bound = any(
                m.get("Destination") == CONTAINER_ROOT
                and m.get("Source") == source
                and m.get("Propagation") == "rshared"
                for m in mounts
            )
            stale = (
                container.status != "running"
                or not bound
                or container.labels.get(FS_LABEL) != "true"
                or container.labels.get(FS_OWNER_LABEL) != settings.CONTAINER_LABEL
                or image not in (container.attrs.get("Config", {}).get("Image"), *container.image.tags)
            )
            if not stale:
                return container.attrs.get("State", {}).get("StartedAt", ""), False
            logger.info(f"cr-fs 配置已变或未运行，重建 | name={self.name} | status={container.status}")
            container.remove(force=True)
        except docker.errors.NotFound:
            pass
        container = self._docker.containers.run(
            image=image,
            name=self.name,
            detach=True,
            privileged=True,
            network_mode="host",
            devices=["/dev/fuse:/dev/fuse:rwm"],
            mounts=[
                docker.types.Mount(
                    target=CONTAINER_ROOT, source=source, type="bind", propagation="rshared"
                )
            ],
            environment={"CRFS_ROOT": CONTAINER_ROOT},
            restart_policy={"Name": "unless-stopped"},
            labels={FS_LABEL: "true", FS_OWNER_LABEL: settings.CONTAINER_LABEL},
        )
        container.reload()
        logger.info(f"cr-fs 已启动 | name={self.name} | root={source} | image={image}")
        return container.attrs.get("State", {}).get("StartedAt", ""), True

    async def ensure(self) -> bool:
        """确保 cr-fs 运行；返回 True 表示其中的挂载已全部丢失（本次新建，或上次之后被重启过），
        依赖旧挂载的 v2 实例须重建。"""
        async with self._ensure_lock:
            generation, created = await asyncio.to_thread(self._ensure_sync)
            restarted = bool(self.generation) and generation != self.generation
            self.generation = generation
            return created or restarted

    def _running_sync(self) -> bool:
        try:
            container = self._docker.containers.get(self.name)
            return container.status == "running"
        except docker.errors.NotFound:
            return False
        except Exception:
            return False

    async def running(self) -> bool:
        return await asyncio.to_thread(self._running_sync)

    def _remove_sync(self) -> None:
        try:
            self._docker.containers.get(self.name).remove(force=True)
        except docker.errors.NotFound:
            pass

    async def remove(self) -> None:
        await asyncio.to_thread(self._remove_sync)

    # ── exec ─────────────────────────────────────────────────────────────────

    def _exec_sync(
        self, cmd: list[str], env: Optional[dict[str, str]] = None
    ) -> tuple[int, bytes, bytes]:
        container = self._docker.containers.get(self.name)
        result = container.exec_run(cmd, environment=env, demux=True)
        out, err = result.output if isinstance(result.output, tuple) else (result.output, b"")
        return result.exit_code, out or b"", err or b""

    async def exec(
        self, cmd: list[str], env: Optional[dict[str, str]] = None
    ) -> tuple[int, bytes, bytes]:
        return await asyncio.to_thread(self._exec_sync, cmd, env)

    async def fsops(self, *args: str, timeout: int = 600) -> dict:
        code, out, err = await self.exec(["timeout", "-k", "5", str(timeout), *FSOPS, *args])
        if code == 124 or code == 137:
            raise FsOpError(code, f"fsops {args[0] if args else ''} 超时（{timeout}s）")
        if code != 0:
            raise FsOpError(code, err.decode(errors="replace").strip() or f"fsops 退出码 {code}")
        return json.loads(out or b"{}")

    def _read_to_file_sync(self, work_area_id: str, root: str, relpath: str, dest: Path) -> ReadResult:
        api = self._docker.api
        exec_id = api.exec_create(
            self.name, [*FSOPS, "read", work_area_id, root, relpath], stdout=True, stderr=True
        )["Id"]
        stream = api.exec_start(exec_id, stream=True, demux=True)
        hasher = hashlib.sha256()
        err = bytearray()
        size = 0
        with open(dest, "wb") as fh:
            for out_chunk, err_chunk in stream:
                if out_chunk:
                    fh.write(out_chunk)
                    hasher.update(out_chunk)
                    size += len(out_chunk)
                if err_chunk:
                    err += err_chunk
        code = api.exec_inspect(exec_id).get("ExitCode")
        text = err.decode(errors="replace")
        if code != 0:
            raise FsOpError(int(code or 1), text.strip() or f"fsops 退出码 {code}")
        stats = [json.loads(line) for line in text.splitlines() if line.startswith("{")]
        if len(stats) != 2:
            raise FsOpError(1, f"fsops read 未返回读前读后状态：{text.strip()!r}")
        before, after = stats
        stable = before == after and after["size"] == size
        return ReadResult(
            path=dest, size=size, mtime_ns=int(after["mtime_ns"]), sha256=hasher.hexdigest(), stable=stable
        )

    async def read_to_file(self, work_area_id: str, root: str, relpath: str, dest: Path) -> ReadResult:
        return await asyncio.to_thread(self._read_to_file_sync, work_area_id, root, relpath, dest)

    # ── 角色目录挂载（rclone）─────────────────────────────────────────────────

    async def mount_status(self) -> dict[str, bool]:
        """``{key: alive}``：cr-fs 内 /srv/mnt/<key> 上的挂载及其是否可访问。

        卡死的 FUSE 挂载会让 stat 阻塞，短超时避免拖住对账与分配。"""
        data = await self.fsops("mounts", timeout=20)
        return {key: bool(info.get("alive")) for key, info in data.get("mounts", {}).items()}

    async def mount(self, spec: RoleMountSpec) -> None:
        key = spec.key
        point = self.container_path("mnt", key)
        cache = self.container_path("cache", key)
        log = self.container_path("run", f"rclone-{key}.log")
        remote = f"minio:{spec.bucket}/{spec.prefix.strip('/')}"
        code, _, err = await self.exec(["mkdir", "-p", point, cache])
        if code != 0:
            raise RuntimeError(f"建挂载点失败 | key={key} | err={err.decode(errors='replace')}")
        cmd = [
            "rclone", "mount", remote, point,
            "--daemon", "--allow-other",
            "--vfs-cache-mode", settings.RCLONE_VFS_CACHE_MODE,
            "--vfs-cache-max-size", settings.RCLONE_VFS_CACHE_MAX_SIZE,
            "--vfs-write-back", settings.RCLONE_VFS_WRITE_BACK,
            "--dir-cache-time", settings.RCLONE_DIR_CACHE_TIME,
            "--cache-dir", cache,
            "--log-level", "INFO", "--log-file", log,
        ]
        if spec.mode == "ro":
            cmd.append("--read-only")
        code, _, err = await self.exec(cmd, env=settings.minio_rclone_env(from_host=True))
        if code != 0:
            raise RuntimeError(
                f"rclone 挂载失败 | key={key} | remote={remote} | err={err.decode(errors='replace')[-500:]}"
            )
        for _ in range(settings.MOUNT_READY_RETRIES):
            status = await self.mount_status()
            if status.get(key):
                logger.info(f"角色目录已挂载 | key={key} | remote={remote} | mode={spec.mode}")
                return
            await asyncio.sleep(settings.MOUNT_READY_INTERVAL)
        raise RuntimeError(f"角色目录挂载未就绪 | key={key} | remote={remote}")

    async def unmount(self, key: str) -> None:
        point = self.container_path("mnt", key)
        cache = self.container_path("cache", key)
        script = (
            f"fusermount3 -u {point} 2>/dev/null || fusermount -u {point} 2>/dev/null "
            f"|| umount -l {point} 2>/dev/null; rmdir {point} 2>/dev/null; rm -rf {cache}; true"
        )
        await self.exec(["sh", "-c", script])
        logger.info(f"角色目录已卸载 | key={key}")

    # ── 自检辅助 ─────────────────────────────────────────────────────────────

    async def probe_propagation(self) -> tuple[bool, str]:
        """在 cr-fs 内挂一个 tmpfs，确认宿主侧可见（rshared 传播生效），随即卸载。"""
        point = self.container_path("run", "probe-mnt")
        host_point = self.host_path("run", "probe-mnt")
        code, _, err = await self.exec(
            ["sh", "-c", f"mkdir -p {point} && (mountpoint -q {point} || mount -t tmpfs -o size=1m crfs-probe {point})"]
        )
        if code != 0:
            return False, f"cr-fs 内挂载失败：{err.decode(errors='replace').strip()}"
        try:
            visible = await asyncio.to_thread(host_point.is_mount)
        finally:
            await self.exec(["sh", "-c", f"umount {point} 2>/dev/null; true"])
        if not visible:
            return False, f"{self.root} 不是 shared 挂载传播，cr-fs 内的挂载在宿主不可见"
        return True, "ok"


class RoleMounts:
    """角色目录挂载的引用计数。同一 key 的挂载/卸载串行；归零后延迟 ``grace`` 秒卸载。"""

    def __init__(self, sidecar: FsSidecar) -> None:
        self._fs = sidecar
        self._refs: dict[str, int] = {}
        self._specs: dict[str, RoleMountSpec] = {}
        self._zero_since: dict[str, float] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._mount_slots = asyncio.Semaphore(_ROLE_MOUNT_CONCURRENCY)

    def host_path(self, key: str) -> Path:
        return self._fs.host_path("mnt", key)

    def refs(self) -> dict[str, int]:
        return dict(self._refs)

    async def acquire(self, specs: Iterable[RoleMountSpec]) -> None:
        """为一个实例取得全部挂载（缺失或失效的就地重挂）；任何一个失败则回滚已取得的引用。"""
        specs = list(specs)
        if not specs:
            return
        taken: list[str] = []
        pending = iter(specs)

        async def worker() -> None:
            for spec in pending:
                await self._acquire_one(spec)
                taken.append(spec.key)

        # 固定数量的工作者不仅限制活跃挂载，也避免大请求创建无界任务。
        tasks = [
            asyncio.create_task(worker())
            for _ in range(min(len(specs), _ROLE_MOUNT_CONCURRENCY))
        ]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            # 等所有工作者退出后再回滚，避免迟到的挂载重新增加引用；取消也必须收尾。
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.release(taken)
            raise

    async def _acquire_one(self, spec: RoleMountSpec) -> None:
        async with self._locks[spec.key]:
            async with self._mount_slots:
                async def prepare() -> None:
                    # 锁内重查：另一实例可能刚建好，同一个旧快照不能指导两次挂载。
                    alive = (await self._fs.mount_status()).get(spec.key)
                    if alive is False:
                        logger.warning(f"角色目录挂载已失效，重挂 | key={spec.key}")
                        await self._fs.unmount(spec.key)
                    if not alive:
                        await self._fs.mount(spec)
                    self._refs[spec.key] = self._refs.get(spec.key, 0) + 1
                    self._specs[spec.key] = spec
                    self._zero_since.pop(spec.key, None)

                # Docker exec 使用线程，取消协程不能取消已经发出的挂载命令。持锁等它真正
                # 结束，再回滚引用；否则下一次分配可能与仍在后台运行的命令重复挂同一目录。
                operation = asyncio.create_task(prepare())
                cancelled = False
                while True:
                    try:
                        await asyncio.shield(operation)
                        break
                    except asyncio.CancelledError:
                        if operation.cancelled():
                            raise
                        cancelled = True
                    except Exception as exc:
                        if cancelled:
                            logger.warning(f"取消期间角色目录挂载失败 | key={spec.key} | err={exc}")
                            raise asyncio.CancelledError from exc
                        raise
                if cancelled:
                    await self.release([spec.key])
                    raise asyncio.CancelledError

    async def release(self, keys: Iterable[str]) -> None:
        now = time.monotonic()
        for key in keys:
            remaining = self._refs.get(key, 0) - 1
            if remaining > 0:
                self._refs[key] = remaining
            else:
                self._refs.pop(key, None)
                self._zero_since[key] = now

    def restore(self, specs: Iterable[RoleMountSpec]) -> None:
        """Hub 重启后收养实例：挂载仍在 cr-fs 里，只恢复引用计数。"""
        for spec in specs:
            self._refs[spec.key] = self._refs.get(spec.key, 0) + 1
            self._specs[spec.key] = spec
            self._zero_since.pop(spec.key, None)

    def reset(self) -> None:
        """cr-fs 重建：所有挂载已丢失。"""
        self._refs.clear()
        self._specs.clear()
        self._zero_since.clear()

    async def sweep(self, grace: float) -> list[str]:
        """卸载引用归零超过 ``grace`` 秒的挂载（含 Hub 重启前遗留、无人引用的挂载）。"""
        now = time.monotonic()
        removed: list[str] = []
        status = await self._fs.mount_status()
        for key in status:
            if self._refs.get(key):
                continue
            since = self._zero_since.setdefault(key, now)
            if now - since < grace:
                continue
            async with self._locks[key]:
                if self._refs.get(key):
                    continue
                await self._fs.unmount(key)
                self._zero_since.pop(key, None)
                self._specs.pop(key, None)
                removed.append(key)
        return removed
