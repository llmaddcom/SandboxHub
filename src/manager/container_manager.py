"""
Docker 容器管理器

职责：封装所有 Docker SDK 操作（run/stop/rm/IP/healthcheck/workspace清理）。
不维护任何状态，是纯操作层。供 WarmPool 调用。
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import docker
import docker.errors
import httpx
from loguru import logger

from src.config import settings
from src.models import ContainerInfo, ManagedContainer, SandboxType, WorkspaceMount

# 挂载容器标签：用于启动恢复时识别并清理孤儿挂载容器（其 registry 映射在重启后已丢失）。
_MOUNTED_LABEL = "sandboxhub.mounted"


def _parse_docker_time(raw: str) -> Optional[datetime]:
    """解析 Docker 的 Created 时间（形如 2026-07-03T02:03:04.123456789Z，纳秒精度）。

    截到微秒再解析；解析失败返回 None（对账侧视为「刚创建」，宁可晚杀不误杀）。
    """
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?", raw or "")
    if not m:
        return None
    base, frac = m.groups()
    micro = (frac or "0")[:6].ljust(6, "0")
    try:
        return datetime.strptime(f"{base}.{micro}", "%Y-%m-%dT%H:%M:%S.%f").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


class ContainerManager:
    """
    Docker 容器操作封装。

    所有 Docker SDK 调用（同步阻塞）通过 asyncio.to_thread 在线程池执行，
    不阻塞 asyncio 事件循环。
    """

    def __init__(self) -> None:
        self._docker = docker.from_env()

    # ── 内部辅助 ─────────────────────────────────────────────────────────────

    def _get_container_ip(self, container) -> str:
        networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
        ip = networks.get(settings.SANDBOX_NETWORK, {}).get("IPAddress", "")
        if ip:
            return ip
        for net_info in networks.values():
            ip = net_info.get("IPAddress", "")
            if ip:
                return ip
        raise RuntimeError(f"无法获取容器 IP | container={container.name}")

    def _build_warm_name(self, sandbox_type: str, slot: int) -> str:
        return f"cr-sb-warm-{sandbox_type}-{slot}-{uuid.uuid4().hex[:6]}"

    def _build_mounted_name(self, sandbox_type: str) -> str:
        return f"cr-sb-mnt-{sandbox_type}-{uuid.uuid4().hex[:8]}"

    def _build_container_env(self, sandbox_type: SandboxType) -> dict:
        # TERM=dumb / PAGER=cat 防止 ANSI 颜色码和交互式分页器污染 LLM 输出
        # 参考 OpenAI Codex unified_exec 的环境变量设计
        env = {
            "TERM": "dumb",
            "NO_COLOR": "1",
            "PAGER": "cat",
            "GIT_PAGER": "cat",
            "GH_PAGER": "cat",
            "DEBIAN_FRONTEND": "noninteractive",  # apt install 静默模式
        }
        # 代理只注入桌面镜像（ubuntu）：它跑 Chrome 等需经宿主代理出网。轻量 code 镜像
        # 直连网络，注入指向 host.docker.internal:8118 的代理反而会让其出网失败（宿主无该代理
        # 或容器到宿主不可达），故 code 一律不注入代理。
        proxy = settings.SANDBOX_HTTP_PROXY
        if proxy and sandbox_type != "code":
            env.update({
                "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy,
                "http_proxy": proxy, "https_proxy": proxy,
                "NO_PROXY": "localhost,127.0.0.1,172.16.0.0/12,10.0.0.0/8",
                "no_proxy": "localhost,127.0.0.1,172.16.0.0/12,10.0.0.0/8",
            })
        return env

    # ── Docker 操作（同步，供 to_thread 调用） ────────────────────────────────

    def _run_container_sync(
        self,
        sandbox_type: SandboxType,
        name: str,
        workspace: Optional[WorkspaceMount] = None,
    ) -> tuple[str, str]:
        """
        docker run，等待 IP，返回 (container_id, container_ip)。
        同步方法，在 asyncio.to_thread 中调用。

        ``workspace`` 非空时，容器额外获得 FUSE 能力（``SYS_ADMIN`` + ``/dev/fuse``）以便
        容器内 rclone 挂载 MinIO，并打上挂载标签供启动恢复识别。
        """
        image = settings.image_for_type(sandbox_type)
        # 清理同名残留
        try:
            self._docker.containers.get(name).remove(force=True)
        except docker.errors.NotFound:
            pass

        labels = {
            settings.CONTAINER_LABEL: "true",
            "sandboxhub.type": sandbox_type,
        }
        if workspace is not None:
            labels[_MOUNTED_LABEL] = "true"

        run_kwargs = dict(
            image=image,
            detach=True,
            name=name,
            network=settings.SANDBOX_NETWORK,
            shm_size="2g",
            # 让容器用 host.docker.internal 指向宿主，代理 URL 不必写死网段
            extra_hosts={"host.docker.internal": "host-gateway"},
            environment=self._build_container_env(sandbox_type),
            labels=labels,
        )
        # 桌面镜像跑 Chrome 需放开 seccomp；轻量 code 镜像无此需求，保持默认 seccomp
        security_opt: list[str] = []
        if sandbox_type == "ubuntu":
            security_opt.append("seccomp=unconfined")
        # 工作区挂载：容器内 rclone 需 FUSE。SYS_ADMIN + /dev/fuse + 放开 apparmor。
        if workspace is not None:
            run_kwargs["cap_add"] = ["SYS_ADMIN"]
            run_kwargs["devices"] = ["/dev/fuse:/dev/fuse:rwm"]
            security_opt.append("apparmor:unconfined")
        if security_opt:
            run_kwargs["security_opt"] = security_opt
        # 自定义 DNS（可选）。use-vc 强制 TCP DNS，应对宿主 UDP 53 被拦截
        dns = settings.dns_servers()
        if dns:
            run_kwargs["dns"] = dns
            run_kwargs["dns_opt"] = ["use-vc"]

        container = self._docker.containers.run(**run_kwargs)

        # 等待 IP 分配（最多 30s）
        deadline = time.time() + 30
        ip = ""
        while time.time() < deadline:
            container.reload()
            if container.status not in ("running", "created"):
                container.remove(force=True)
                raise RuntimeError(f"容器意外退出 | name={name} | status={container.status}")
            try:
                ip = self._get_container_ip(container)
                break
            except RuntimeError:
                time.sleep(0.5)

        if not ip:
            container.remove(force=True)
            raise RuntimeError(f"容器无法获取 IP | name={name}")

        return container.id, ip

    def _stop_and_remove_sync(self, container_id: str) -> None:
        """停止并删除容器。同步方法，在 asyncio.to_thread 中调用。"""
        try:
            c = self._docker.containers.get(container_id)
            c.remove(force=True)
        except docker.errors.NotFound:
            pass
        except Exception as e:
            logger.warning(f"删除容器失败 | id={container_id} | err={e}")

    def _list_managed_sync(self) -> list[ManagedContainer]:
        """列出所有 sandboxhub.managed=true 的容器（含非 running 的），供对账使用。"""
        try:
            containers = self._docker.containers.list(
                all=True,
                filters={"label": f"{settings.CONTAINER_LABEL}=true"},
            )
        except Exception as e:
            logger.warning(f"列出管理容器失败: {e}")
            return []
        result: list[ManagedContainer] = []
        for c in containers:
            try:
                ip = ""
                if c.status == "running":
                    try:
                        ip = self._get_container_ip(c)
                    except RuntimeError:
                        ip = ""
                result.append(
                    ManagedContainer(
                        container_id=c.id,
                        container_name=c.name,
                        status=c.status,
                        sandbox_type=c.labels.get("sandboxhub.type", "ubuntu"),
                        mounted=c.labels.get(_MOUNTED_LABEL) == "true",
                        created_at=_parse_docker_time(c.attrs.get("Created", "")),
                        container_ip=ip,
                    )
                )
            except Exception as e:
                logger.warning(f"读取容器信息失败，本轮跳过 | err={e}")
        return result

    # ── 异步公开接口 ──────────────────────────────────────────────────────────

    async def run_container(
        self,
        sandbox_type: SandboxType,
        slot: int = 0,
        workspace: Optional[WorkspaceMount] = None,
    ) -> ContainerInfo:
        """
        启动新容器，等待健康检查，返回 ContainerInfo。
        冷启动路径，在 asyncio.to_thread 中执行 Docker 操作。

        ``workspace`` 非空时：容器带 FUSE 能力启动，健康后在容器内用 rclone 把
        MinIO ``bucket/prefix`` 挂到 ``mount_path``；挂载失败即销毁容器并抛错。
        """
        mounted = workspace is not None
        name = (
            self._build_mounted_name(sandbox_type)
            if mounted
            else self._build_warm_name(sandbox_type, slot)
        )
        container_id, ip = await asyncio.to_thread(
            self._run_container_sync, sandbox_type, name, workspace
        )
        # 等待 API 就绪
        if not await self.wait_healthy(ip):
            await asyncio.to_thread(self._stop_and_remove_sync, container_id)
            raise RuntimeError(f"容器健康检查超时 | name={name}")

        if mounted:
            try:
                await self.mount_workspace(container_id, workspace)
            except Exception as e:
                # 挂载失败不可降级为「无挂载容器」：会让容器内写入丢失、createrole 读不到。
                await asyncio.to_thread(self._stop_and_remove_sync, container_id)
                raise RuntimeError(f"工作区挂载失败 | name={name} | err={e}")

        logger.info(
            f"容器就绪 | name={name} | ip={ip}"
            + (f" | mounted={workspace.bucket}/{workspace.prefix}" if mounted else "")
        )
        return ContainerInfo(
            container_id=container_id,
            container_name=name,
            container_ip=ip,
            sandbox_type=sandbox_type,
            mounted=mounted,
            mount_path=workspace.mount_path if mounted else "/workspace",
        )

    # ── 工作区挂载（容器内 rclone over MinIO/S3）─────────────────────────────

    async def mount_workspace(self, container_id: str, workspace: WorkspaceMount) -> None:
        """在容器内用 rclone 把 MinIO bucket/prefix 实时挂到 mount_path，并等待挂载就绪。"""
        await asyncio.to_thread(self._mount_workspace_sync, container_id, workspace)
        if not await self._wait_mounted(container_id, workspace.mount_path):
            raise RuntimeError(f"挂载点未就绪 | path={workspace.mount_path}")

    def _mount_workspace_sync(self, container_id: str, workspace: WorkspaceMount) -> None:
        """启动容器内 rclone mount（后台进程）。同步方法，在 asyncio.to_thread 中调用。

        rclone 的 S3(MinIO) remote 经 ``RCLONE_CONFIG_MINIO_*`` 环境变量内联，凭据不落盘。
        ``--vfs-cache-mode full``（默认）对 rename/并发读写支持最完整——writes 模式下
        写回窗口内的 rename-over（sed -i 等）会报 EIO（issue #9）；``--vfs-cache-max-size``
        限制本地缓存体积。短 ``--vfs-write-back`` 使文件关闭后近实时回写 MinIO；
        ``--dir-cache-time`` 短使 MinIO 侧新增对象较快在容器内可见。容器与 rclone 均 root，
        无需 ``--allow-other``。
        """
        container = self._docker.containers.get(container_id)
        mount_path = workspace.mount_path
        remote = f"minio:{workspace.bucket}/{workspace.prefix}".rstrip("/")
        cmd = (
            f"mkdir -p {mount_path} && "
            f"rclone mount {remote} {mount_path} "
            f"--vfs-cache-mode {settings.RCLONE_VFS_CACHE_MODE} "
            f"--vfs-cache-max-size {settings.RCLONE_VFS_CACHE_MAX_SIZE} "
            f"--vfs-write-back {settings.RCLONE_VFS_WRITE_BACK} "
            f"--dir-cache-time {settings.RCLONE_DIR_CACHE_TIME} "
            f"--poll-interval {settings.RCLONE_DIR_CACHE_TIME} "
            f"--log-level INFO --log-file /tmp/rclone-mount.log"
        )
        # detach=True：rclone 作为容器内常驻前台进程后台运行；不退出即维持挂载。
        container.exec_run(
            cmd=["/bin/sh", "-lc", cmd],
            environment=settings.minio_rclone_env(),
            detach=True,
            privileged=False,
        )

    async def _wait_mounted(self, container_id: str, mount_path: str) -> bool:
        """轮询 ``mountpoint -q`` 直到挂载点就绪或超时。"""
        for _ in range(settings.MOUNT_READY_RETRIES):
            ok = await asyncio.to_thread(self._is_mountpoint_sync, container_id, mount_path)
            if ok:
                return True
            await asyncio.sleep(settings.MOUNT_READY_INTERVAL)
        return False

    def _is_mountpoint_sync(self, container_id: str, mount_path: str) -> bool:
        try:
            container = self._docker.containers.get(container_id)
            res = container.exec_run(cmd=["/bin/sh", "-lc", f"mountpoint -q {mount_path}"])
            return res.exit_code == 0
        except Exception:
            return False

    def _unmount_workspace_sync(self, container_id: str, mount_path: str) -> None:
        """卸载容器内 rclone 挂载（销毁容器前调用，避免误把 MinIO 数据当本地文件清理）。"""
        try:
            container = self._docker.containers.get(container_id)
            container.exec_run(
                cmd=[
                    "/bin/sh", "-lc",
                    # fuse3 提供 fusermount3；兼容 fuse(v2) 的 fusermount 与 umount 兜底。
                    f"fusermount3 -u {mount_path} 2>/dev/null || "
                    f"fusermount -u {mount_path} 2>/dev/null || "
                    f"umount {mount_path} 2>/dev/null || true",
                ],
            )
        except docker.errors.NotFound:
            pass
        except Exception as e:
            logger.warning(f"卸载工作区失败（容器仍将销毁）| id={container_id} | err={e}")

    async def unmount_workspace(self, container_id: str, mount_path: str) -> None:
        await asyncio.to_thread(self._unmount_workspace_sync, container_id, mount_path)

    async def remove_container(self, container_id: str) -> None:
        """异步删除容器。"""
        await asyncio.to_thread(self._stop_and_remove_sync, container_id)

    async def is_healthy(self, container_ip: str, timeout: float = 2.0) -> bool:
        """TCP 探测容器 API 端口是否可连接。"""
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(container_ip, settings.SANDBOX_API_PORT),
                timeout=timeout,
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True
        except Exception:
            return False

    async def wait_healthy(self, container_ip: str, timeout: int = 30) -> bool:
        """轮询直到健康或超时，返回是否成功。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if await self.is_healthy(container_ip):
                return True
            await asyncio.sleep(1)
        return False

    async def clean_and_reset(self, container_ip: str) -> None:
        """
        清理 workspace + 重置 bash session（归还 pool / 启动收养回 pool 前调用）。
        失败抛 RuntimeError，由调用方销毁容器——上一任租户的文件与会话状态未清干净的
        容器绝不能回 pool（跨租户串台）。
        """
        api_base = f"http://{container_ip}:{settings.SANDBOX_API_PORT}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.post(f"{api_base}/api/terminal/restart")
                await client.post(
                    f"{api_base}/api/terminal/execute",
                    json={"command": "rm -rf /workspace/* 2>/dev/null; true", "timeout": 10},
                )
        except Exception as e:
            raise RuntimeError(f"clean_and_reset 失败 | ip={container_ip} | err={e}") from e

    async def list_managed(self) -> list[ManagedContainer]:
        """列出 Docker 侧全部受管容器（含已停止的），供 Reconciler 对账。"""
        return await asyncio.to_thread(self._list_managed_sync)
