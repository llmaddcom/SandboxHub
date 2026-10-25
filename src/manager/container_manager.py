"""
Docker 容器管理器

职责：封装所有 Docker SDK 操作（run/stop/rm/IP/healthcheck/workspace清理）。
不维护任何状态，是纯操作层。供 WarmPool 调用。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import docker
import docker.errors
import httpx
from loguru import logger

from src.config import settings
from src.models import (
    MOUNT_LAYOUT_LABEL,
    ROLE_LABEL,
    SANDBOX_ID_LABEL,
    SESSION_LABEL,
    USER_LABEL,
    V2_LABEL,
    WORK_AREA_LABEL,
    ContainerInfo,
    ManagedContainer,
    NetworkMode,
    SandboxType,
    WorkspaceMount,
)

# 挂载容器标签：用于启动恢复时识别并清理孤儿挂载容器（其 registry 映射在重启后已丢失）。
_MOUNTED_LABEL = "sandboxhub.mounted"
# env 注入容器标签：注入过调用方环境变量（可能含租户凭据）的容器，同挂载容器一样
# 专属化——不入 warm pool、启动恢复不收养、release 即销毁（env 无法从运行中容器清除）。
_ENV_LABEL = "sandboxhub.env-injected"
# 网关容器标签：cr-host 不带 CONTAINER_LABEL（不是沙盒、不参与对账），单独标识。
_GATEWAY_LABEL = "sandboxhub.gateway"
# 容器内网络策略标记文件：环境变量不能热改，切网后写文件供镜像脚本 / MANIFEST 感知。
_POLICY_MARKER = "/etc/sandbox/network-policy"
# v2 实例镜像须带此标签：容器内 API 会校验每实例 token（SANDBOX_API_TOKEN）。
API_AUTH_LABEL = "sandboxhub.api_auth"
# 镜像能力标签（SandboxHub#52）：app 提供 /api/procs 长驻进程接口（"1"）；预置的执行引擎及版本
# （JSON 对象，如 {"pi":"1.4.0","dsh":"0.2.1-rc.3"}）。经 /v2/capabilities 的 procs / engines 上报。
PROCS_LABEL = "sandboxhub.procs"
ENGINES_LABEL = "sandboxhub.engines"
# v2 实例去掉的默认 capability：NET_RAW 可伪造 ARP/原始包，MKNOD 可建设备节点。
_V2_CAP_DROP = ["NET_RAW", "MKNOD"]


@dataclass(frozen=True)
class Bind:
    """v2 实例的一条宿主 bind。``propagation`` 仅角色目录（FUSE 挂载）用 rslave。"""
    source: str
    target: str
    read_only: bool
    propagation: str = ""


@dataclass(frozen=True)
class BridgeInfo:
    """一张 v2 网络在宿主侧的事实，供 netguard 下发规则与自检。"""
    network: str
    bridge: str       # 宿主网桥接口名（br-<id[:12]> 或 bridge.name 选项）
    host_ip: str      # 网桥在宿主侧的地址（实例看到的默认网关）
    gateway_ip: str   # v2 网关容器在该网上的 IP（唯一放行对象）
    internal: bool


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

    @property
    def docker(self) -> docker.DockerClient:
        return self._docker

    # ── 内部辅助 ─────────────────────────────────────────────────────────────

    def _get_container_ip(self, container, prefer: Optional[str] = None) -> str:
        networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
        ip = networks.get(prefer or settings.SANDBOX_NETWORK, {}).get("IPAddress", "")
        if ip:
            return ip
        for net_info in networks.values():
            ip = net_info.get("IPAddress", "")
            if ip:
                return ip
        raise RuntimeError(f"无法获取容器 IP | container={container.name}")

    @staticmethod
    def _container_network(container) -> str:
        """容器实际所在网络名：优先受管网络（v1 两张 + v2 两张），其次任一已接入网络；未接入返回空。"""
        networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
        for name in (
            settings.SANDBOX_NETWORK,
            settings.SANDBOX_NETWORK_ISOLATED,
            settings.wa_network,
            settings.wa_network_isolated,
        ):
            if name in networks:
                return name
        return next(iter(networks), "")

    def _build_warm_name(self, sandbox_type: str, slot: int) -> str:
        return f"cr-sb-warm-{sandbox_type}-{slot}-{uuid.uuid4().hex[:6]}"

    def _build_mounted_name(self, sandbox_type: str) -> str:
        return f"cr-sb-mnt-{sandbox_type}-{uuid.uuid4().hex[:8]}"

    def _build_env_name(self, sandbox_type: str) -> str:
        return f"cr-sb-env-{sandbox_type}-{uuid.uuid4().hex[:8]}"

    def _build_container_env(
        self, sandbox_type: SandboxType, extra_env: Optional[dict[str, str]] = None
    ) -> dict:
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
        # DNS 保留开关：配置了 SANDBOX_DNS（经 docker --dns 注入 resolv.conf）或显式开启
        # SANDBOX_KEEP_DNS 时，通知镜像 entrypoint 跳过 resolv.conf 覆写（ubuntu 镜像
        # entrypoint 默认覆写为 8.8.8.8/1.1.1.1，会顶掉 --dns 配置，离线机上解析全超时）。
        if settings.SANDBOX_KEEP_DNS or settings.dns_servers():
            env["SANDBOX_KEEP_DNS"] = "1"
        # 调用方注入的业务环境变量（issue #15/#16：CR_API_BASE、CR_SANDBOX_TOKEN 等）。
        # 最后合并、允许覆盖默认值；值可能含凭据，任何日志只记 key 不记 value。
        if extra_env:
            env.update(extra_env)
        # 系统业务上限只能由 system.yaml 控制；0 明确表示不限制文件大小。
        env["SANDBOX_FILE_UPLOAD_MAX_BYTES"] = str(max(0, settings.FILE_UPLOAD_MAX_BYTES))
        return env

    # ── Docker 操作（同步，供 to_thread 调用） ────────────────────────────────

    def _run_container_sync(
        self,
        sandbox_type: SandboxType,
        name: str,
        workspace: Optional[WorkspaceMount] = None,
        extra_env: Optional[dict[str, str]] = None,
        network: Optional[str] = None,
    ) -> tuple[str, str]:
        """
        docker run，等待 IP，返回 (container_id, container_ip)。
        同步方法，在 asyncio.to_thread 中调用。

        ``network`` 指定容器落在哪张 Docker 网络（默认联网态 SANDBOX_NETWORK；策略 deny 的
        冷启动直接落隔离网，省一次切换）。

        ``workspace`` 非空时，容器额外获得 FUSE 能力（``SYS_ADMIN`` + ``/dev/fuse``）以便
        容器内 rclone 挂载 MinIO，并打上挂载标签供启动恢复识别。
        ``extra_env`` 为调用方注入的环境变量（仅创建时生效，值不写日志）。
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
        if extra_env:
            labels[_ENV_LABEL] = "true"

        network = network or settings.SANDBOX_NETWORK
        run_kwargs = dict(
            image=image,
            detach=True,
            name=name,
            network=network,
            shm_size="2g",
            # 让容器用 host.docker.internal 指向宿主，代理 URL 不必写死网段
            extra_hosts={"host.docker.internal": "host-gateway"},
            environment=self._build_container_env(sandbox_type, extra_env),
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
                ip = self._get_container_ip(container, prefer=network)
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
                token = ""
                if c.labels.get(V2_LABEL) == "true":
                    env = c.attrs.get("Config", {}).get("Env") or []
                    token = next(
                        (e.split("=", 1)[1] for e in env if e.startswith("SANDBOX_API_TOKEN=")), ""
                    )
                result.append(
                    ManagedContainer(
                        container_id=c.id,
                        container_name=c.name,
                        status=c.status,
                        sandbox_type=c.labels.get("sandboxhub.type", "ubuntu"),
                        mounted=c.labels.get(_MOUNTED_LABEL) == "true",
                        created_at=_parse_docker_time(c.attrs.get("Created", "")),
                        container_ip=ip,
                        env_injected=c.labels.get(_ENV_LABEL) == "true",
                        network=self._container_network(c),
                        labels=dict(c.labels),
                        api_token=token,
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
        extra_env: Optional[dict[str, str]] = None,
        network: Optional[str] = None,
    ) -> ContainerInfo:
        """
        启动新容器，等待健康检查，返回 ContainerInfo。
        冷启动路径，在 asyncio.to_thread 中执行 Docker 操作。

        ``network`` 非空时容器直接落该网络（策略 deny 的冷启动落隔离网）；落隔离网的容器
        健康后写入网络策略标记文件。

        ``workspace`` 非空时：容器带 FUSE 能力启动，健康后在容器内用 rclone 把
        MinIO ``bucket/prefix`` 挂到 ``mount_path``；挂载失败即销毁容器并抛错。
        ``extra_env`` 非空时：键值对注入为容器环境变量（issue #15/#16，仅创建时
        生效；值可能是凭据，日志绝不打印 value）。
        """
        mounted = workspace is not None
        if mounted:
            name = self._build_mounted_name(sandbox_type)
        elif extra_env:
            name = self._build_env_name(sandbox_type)
        else:
            name = self._build_warm_name(sandbox_type, slot)
        network = network or settings.SANDBOX_NETWORK
        container_id, ip = await asyncio.to_thread(
            self._run_container_sync, sandbox_type, name, workspace, extra_env, network
        )
        # 等待 API 就绪
        if not await self.wait_healthy(ip):
            await asyncio.to_thread(self._stop_and_remove_sync, container_id)
            raise RuntimeError(f"容器健康检查超时 | name={name}")
        if network == settings.SANDBOX_NETWORK_ISOLATED:
            await asyncio.to_thread(self._write_policy_marker_sync, container_id, "deny")

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
            env_injected=bool(extra_env),
            network=network,
        )

    # ── 网络策略：受管网络 / 网关容器 / 运行中容器热切网（SandboxHub#42）──────

    def _ensure_network_sync(self, name: str, internal: bool) -> None:
        try:
            self._docker.networks.get(name)
        except docker.errors.NotFound:
            self._docker.networks.create(
                name, driver="bridge", internal=internal, labels={_GATEWAY_LABEL: "network"}
            )
            logger.info(f"已创建 Docker 网络 | name={name} | internal={internal}")

    async def ensure_networks(self) -> None:
        """幂等创建联网态网络与隔离网。SANDBOX_NETWORK 是 Docker 内置网络时网络策略不可用，只告警。"""
        if not settings.network_policy_supported:
            logger.warning(
                f"SANDBOX_NETWORK={settings.SANDBOX_NETWORK!r} 是 Docker 内置网络，无内嵌 DNS，"
                "网络策略不可用：acquire 请求 deny 将被拒绝。改配用户自定义网络（默认 cr-sb-net）即可启用"
            )
            return
        await asyncio.to_thread(self._ensure_network_sync, settings.SANDBOX_NETWORK, False)
        await asyncio.to_thread(self._ensure_network_sync, settings.SANDBOX_NETWORK_ISOLATED, True)

    def _ensure_gateway_sync(self) -> None:
        """确保网关容器 cr-host 运行且同时接在两张网上；配置（转发表 / 镜像）变了就重建。"""
        name = settings.SANDBOX_GATEWAY_NAME
        image = settings.DOCKER_IMAGE_GATEWAY
        forwards = ",".join(f"{port}={upstream}" for port, upstream in settings.gateway_forwards())
        container = None
        try:
            container = self._docker.containers.get(name)
            container.reload()
            env = container.attrs.get("Config", {}).get("Env") or []
            current_forwards = next((e.split("=", 1)[1] for e in env if e.startswith("FORWARDS=")), None)
            stale = (
                container.status != "running"
                or current_forwards != forwards
                or container.labels.get(_GATEWAY_LABEL) != "true"
                or image not in (container.attrs.get("Config", {}).get("Image"), *container.image.tags)
            )
            if stale:
                logger.info(f"网关容器配置已变或未运行，重建 | name={name} | status={container.status}")
                container.remove(force=True)
                container = None
        except docker.errors.NotFound:
            container = None
        if container is None:
            container = self._docker.containers.run(
                image=image,
                detach=True,
                name=name,
                network=settings.SANDBOX_NETWORK,
                environment={"FORWARDS": forwards},
                extra_hosts={"host.docker.internal": "host-gateway"},
                restart_policy={"Name": "unless-stopped"},
                labels={_GATEWAY_LABEL: "true"},
            )
            logger.info(f"网关容器已启动 | name={name} | forwards={forwards or '(空)'}")
        container.reload()
        attached = container.attrs.get("NetworkSettings", {}).get("Networks", {})
        if settings.SANDBOX_NETWORK_ISOLATED not in attached:
            self._docker.networks.get(settings.SANDBOX_NETWORK_ISOLATED).connect(container)

    async def ensure_gateway(self) -> None:
        """幂等确保网关容器就绪（启动时与每轮对账调用）。网络策略不可用时不建网关。"""
        if not settings.network_policy_supported:
            return
        await asyncio.to_thread(self._ensure_gateway_sync)

    def _remove_gateway_sync(self) -> None:
        try:
            self._docker.containers.get(settings.SANDBOX_GATEWAY_NAME).remove(force=True)
        except docker.errors.NotFound:
            pass
        except Exception as e:
            logger.warning(f"删除网关容器失败 | err={e}")

    async def remove_gateway(self) -> None:
        await asyncio.to_thread(self._remove_gateway_sync)

    def _switch_network_sync(self, container_id: str, target: str) -> str:
        """把运行中容器从当前网络挪到 ``target``，返回新 IP。同步方法，在 to_thread 中调用。

        先断开除 target 外的全部网络再接入 target（容器任一时刻只在一张受管网络上），
        进程与 tmux 会话不受影响，仅在途 TCP 连接断开。
        """
        container = self._docker.containers.get(container_id)
        container.reload()
        attached = list(container.attrs.get("NetworkSettings", {}).get("Networks", {}))
        for name in attached:
            if name != target:
                self._docker.networks.get(name).disconnect(container, force=True)
        if target not in attached:
            self._docker.networks.get(target).connect(container)
        container.reload()
        return self._get_container_ip(container, prefer=target)

    def _write_policy_marker_sync(self, container_id: str, mode: NetworkMode) -> None:
        """写容器内 /etc/sandbox/network-policy（best-effort，失败只记 warning）。"""
        try:
            container = self._docker.containers.get(container_id)
            container.exec_run(
                cmd=["/bin/sh", "-c", f"mkdir -p /etc/sandbox && printf '%s\\n' {mode} > {_POLICY_MARKER}"],
            )
        except Exception as e:
            logger.warning(f"写网络策略标记失败 | id={container_id} | err={e}")

    async def switch_network(self, info: ContainerInfo, mode: NetworkMode) -> ContainerInfo:
        """按策略把容器热切到对应网络，原地更新 ``info`` 的 ip/network 并返回。

        目标网络与在册网络一致时不动 Docker（对账器负责把在册状态与实际对齐）。
        切网后等待容器 API 在新 IP 上可达；不可达抛 RuntimeError，由调用方销毁重建。
        """
        target = settings.network_for_mode(mode)
        if info.network == target:
            return info
        old_ip = info.container_ip
        new_ip = await asyncio.to_thread(self._switch_network_sync, info.container_id, target)
        if not await self.wait_healthy(new_ip, timeout=10):
            raise RuntimeError(f"切网后容器不可达 | name={info.container_name} | ip={new_ip}")
        await asyncio.to_thread(self._write_policy_marker_sync, info.container_id, mode)
        info.container_ip = new_ip
        info.network = target
        logger.info(
            f"容器已切网 | name={info.container_name} | mode={mode} | network={target} "
            f"| ip={old_ip}->{new_ip}"
        )
        return info

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
        回收长驻进程 + 清理 workspace + 重置 bash session（归还 pool / 启动收养回 pool 前调用）。
        失败抛 RuntimeError，由调用方销毁容器——上一任租户的文件、会话状态与进程未清干净的
        容器绝不能回 pool（跨租户串台）。
        """
        api_base = f"http://{container_ip}:{settings.SANDBOX_API_PORT}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                await self._remove_procs(client, api_base)
                await client.post(f"{api_base}/api/terminal/restart")
                await client.post(
                    f"{api_base}/api/terminal/execute",
                    json={"command": "rm -rf /workspace/* 2>/dev/null; true", "timeout": 10},
                )
        except Exception as e:
            raise RuntimeError(f"clean_and_reset 失败 | ip={container_ip} | err={e}") from e

    @staticmethod
    async def _remove_procs(client: httpx.AsyncClient, api_base: str) -> None:
        """上一任租户的长驻进程（/api/procs，SandboxHub#52）不能留给下一任：逐个 DELETE（先 KILL 进程组）。
        旧镜像没有该接口（404）视为没有；其余失败抛出，由调用方销毁容器。"""
        response = await client.get(f"{api_base}/api/procs")
        if response.status_code == 404:
            return
        response.raise_for_status()
        for proc in response.json().get("procs", []):
            removed = await client.delete(f"{api_base}/api/procs/{proc['proc_id']}")
            if removed.status_code != 404:   # 404 = 已被回收
                removed.raise_for_status()

    async def list_managed(self) -> list[ManagedContainer]:
        """列出 Docker 侧全部受管容器（含已停止的），供 Reconciler 对账。"""
        return await asyncio.to_thread(self._list_managed_sync)

    # ── v2：按工作区分配的执行实例（ADR 0025）──────────────────────────────────

    def _ensure_wa_network_sync(self, name: str, internal: bool) -> None:
        """v2 网络：关闭 ICC，实例之间在网桥上互不可达（网关例外由 netguard 放行）。"""
        try:
            network = self._docker.networks.get(name)
            icc = (network.attrs.get("Options") or {}).get("com.docker.network.bridge.enable_icc")
            if icc != "false":
                raise RuntimeError(
                    f"v2 网络 {name} 已存在但未关闭 ICC；请删除后由 Hub 重建（docker network rm {name}）"
                )
        except docker.errors.NotFound:
            self._docker.networks.create(
                name,
                driver="bridge",
                internal=internal,
                options={"com.docker.network.bridge.enable_icc": "false"},
                labels={_GATEWAY_LABEL: "network", V2_LABEL: "true"},
            )
            logger.info(f"已创建 v2 网络 | name={name} | internal={internal} | icc=false")

    async def ensure_wa_networks(self) -> None:
        await asyncio.to_thread(self._ensure_wa_network_sync, settings.wa_network, False)
        await asyncio.to_thread(self._ensure_wa_network_sync, settings.wa_network_isolated, True)

    def _ensure_wa_gateway_sync(self) -> bool:
        """v2 网关：与 v1 同镜像，但不转发 MinIO；在两张 v2 网上带别名 SANDBOX_GATEWAY_NAME。
        返回 True 表示本次新建（IP 可能变了，netguard 须重新下发）。"""
        name = settings.wa_gateway_name
        alias = settings.SANDBOX_GATEWAY_NAME
        image = settings.DOCKER_IMAGE_GATEWAY
        forwards = ",".join(
            f"{port}={upstream}" for port, upstream in settings.gateway_forwards(include_minio=False)
        )
        created = False
        try:
            container = self._docker.containers.get(name)
            container.reload()
            env = container.attrs.get("Config", {}).get("Env") or []
            current = next((e.split("=", 1)[1] for e in env if e.startswith("FORWARDS=")), None)
            if (
                container.status != "running"
                or current != forwards
                or container.labels.get(_GATEWAY_LABEL) != "true"
                or image not in (container.attrs.get("Config", {}).get("Image"), *container.image.tags)
            ):
                logger.info(f"v2 网关配置已变或未运行，重建 | name={name} | status={container.status}")
                container.remove(force=True)
                raise docker.errors.NotFound("recreate")
        except docker.errors.NotFound:
            endpoint = self._docker.api.create_endpoint_config(aliases=[alias])
            container = self._docker.containers.run(
                image=image,
                detach=True,
                name=name,
                network=settings.wa_network,
                networking_config={settings.wa_network: endpoint},
                environment={"FORWARDS": forwards},
                extra_hosts={"host.docker.internal": "host-gateway"},
                restart_policy={"Name": "unless-stopped"},
                labels={_GATEWAY_LABEL: "true", V2_LABEL: "true"},
            )
            created = True
            logger.info(f"v2 网关已启动 | name={name} | alias={alias} | forwards={forwards or '(空)'}")
        container.reload()
        attached = container.attrs.get("NetworkSettings", {}).get("Networks", {})
        if settings.wa_network_isolated not in attached:
            self._docker.networks.get(settings.wa_network_isolated).connect(container, aliases=[alias])
            created = True
        return created

    async def ensure_wa_gateway(self) -> bool:
        return await asyncio.to_thread(self._ensure_wa_gateway_sync)

    def _wa_bridges_sync(self) -> list[BridgeInfo]:
        gateway = self._docker.containers.get(settings.wa_gateway_name)
        gateway.reload()
        attached = gateway.attrs.get("NetworkSettings", {}).get("Networks", {})
        infos: list[BridgeInfo] = []
        for name in (settings.wa_network, settings.wa_network_isolated):
            network = self._docker.networks.get(name)
            attrs = network.attrs
            options = attrs.get("Options") or {}
            bridge = options.get("com.docker.network.bridge.name") or f"br-{attrs['Id'][:12]}"
            ipam = (attrs.get("IPAM") or {}).get("Config") or [{}]
            host_ip = next((c.get("Gateway") for c in ipam if c.get("Gateway")), "")
            gateway_ip = (attached.get(name) or {}).get("IPAddress", "")
            if not gateway_ip:
                raise RuntimeError(f"v2 网关不在网络 {name} 上")
            infos.append(
                BridgeInfo(
                    network=name,
                    bridge=bridge,
                    host_ip=host_ip,
                    gateway_ip=gateway_ip,
                    internal=bool(attrs.get("Internal")),
                )
            )
        return infos

    async def wa_bridges(self) -> list[BridgeInfo]:
        return await asyncio.to_thread(self._wa_bridges_sync)

    def _image_labels_sync(self, image: str) -> Optional[dict]:
        try:
            return self._docker.images.get(image).labels or {}
        except docker.errors.ImageNotFound:
            return None

    async def image_labels(self, image: str) -> Optional[dict]:
        """镜像标签；镜像不存在返回 None。"""
        return await asyncio.to_thread(self._image_labels_sync, image)

    def _build_wa_env(
        self, sandbox_type: SandboxType, api_token: str, extra_env: Optional[dict[str, str]]
    ) -> dict[str, str]:
        env = self._build_container_env(sandbox_type, extra_env)
        env.update(
            {
                "SANDBOX_API_TOKEN": api_token,
                "SANDBOX_CWD": "/workspace/work",
                "SANDBOX_WORKSPACE": "/workspace/work",
                "SANDBOX_LAYOUT": "work-area/v1",
                "CR_SKILLS_DIR": "/workspace/role/skills",
                "SANDBOX_JOB_MAX_LIFETIME": str(max(0, settings.WORK_AREA_JOB_MAX_LIFETIME)),
            }
        )
        return env

    def _run_wa_container_sync(
        self,
        *,
        sandbox_id: str,
        work_area_id: str,
        user_id: str,
        role_id: str,
        session_id: str,
        sandbox_type: SandboxType,
        api_token: str,
        binds: list[Bind],
        mount_layout: list[dict],
        extra_env: Optional[dict[str, str]],
        network: str,
        limits: dict,
    ) -> tuple[str, str, str]:
        image = settings.image_v2_for_type(sandbox_type)
        name = f"cr-wa-{sandbox_type}-{sandbox_id.removeprefix('sb_')}"
        labels = {
            settings.CONTAINER_LABEL: "true",
            "sandboxhub.type": sandbox_type,
            V2_LABEL: "true",
            WORK_AREA_LABEL: work_area_id,
            SANDBOX_ID_LABEL: sandbox_id,
            USER_LABEL: user_id,
            ROLE_LABEL: role_id,
            SESSION_LABEL: session_id,
            MOUNT_LAYOUT_LABEL: json.dumps(mount_layout, ensure_ascii=False, separators=(",", ":")),
        }
        if extra_env:
            labels[_ENV_LABEL] = "true"
        mounts = [
            docker.types.Mount(
                target=b.target,
                source=b.source,
                type="bind",
                read_only=b.read_only,
                propagation=b.propagation or None,
            )
            for b in binds
        ]
        security_opt = ["no-new-privileges"]
        if sandbox_type == "ubuntu":
            security_opt.append("seccomp=unconfined")
        run_kwargs = dict(
            image=image,
            detach=True,
            name=name,
            network=network,
            shm_size="2g",
            extra_hosts={"host.docker.internal": "host-gateway"},
            environment=self._build_wa_env(sandbox_type, api_token, extra_env),
            labels=labels,
            mounts=mounts,
            cap_drop=_V2_CAP_DROP,
            security_opt=security_opt,
            # 不改容器工作目录：镜像入口依赖 WORKDIR（/app、/root）启动 app；终端初始 cwd 由 SANDBOX_CWD 决定
        )
        if limits.get("memory_mb"):
            run_kwargs["mem_limit"] = f"{int(limits['memory_mb'])}m"
        if limits.get("cpus"):
            run_kwargs["nano_cpus"] = int(float(limits["cpus"]) * 1e9)
        if limits.get("pids"):
            run_kwargs["pids_limit"] = int(limits["pids"])
        dns = settings.dns_servers()
        if dns:
            run_kwargs["dns"] = dns
            run_kwargs["dns_opt"] = ["use-vc"]

        try:
            container = self._docker.containers.run(**run_kwargs)
        except docker.errors.APIError:
            # 创建成功但启动失败（如 bind 源不可用）会留下 created 状态的容器；名字含随机 ID，只会是自己的
            try:
                self._docker.containers.get(name).remove(force=True)
            except docker.errors.APIError:
                pass
            raise
        deadline = time.time() + 30
        ip = ""
        while time.time() < deadline:
            container.reload()
            if container.status not in ("running", "created"):
                logs = container.logs(tail=20).decode(errors="replace")
                container.remove(force=True)
                raise RuntimeError(f"v2 实例意外退出 | name={name} | status={container.status} | logs={logs[-500:]}")
            try:
                ip = self._get_container_ip(container, prefer=network)
                break
            except RuntimeError:
                time.sleep(0.3)
        if not ip:
            container.remove(force=True)
            raise RuntimeError(f"v2 实例无法获取 IP | name={name}")
        return container.id, name, ip

    def _container_logs_sync(self, container_id: str) -> str:
        try:
            raw = self._docker.containers.get(container_id).logs(tail=20)
            return raw.decode(errors="replace")[-500:]
        except docker.errors.APIError:
            return ""

    async def run_wa_container(self, **kwargs) -> ContainerInfo:
        """创建 v2 实例：无特权、只有 bind 列表、每实例 token；不进 warm pool。等待 API 可连。"""
        sandbox_type: SandboxType = kwargs["sandbox_type"]
        network: str = kwargs["network"]
        container_id, name, ip = await asyncio.to_thread(lambda: self._run_wa_container_sync(**kwargs))
        if not await self.wait_healthy(ip):
            logs = await asyncio.to_thread(self._container_logs_sync, container_id)
            await asyncio.to_thread(self._stop_and_remove_sync, container_id)
            raise RuntimeError(f"v2 实例健康检查超时 | name={name} | logs={logs}")
        if network == settings.wa_network_isolated:
            await asyncio.to_thread(self._write_policy_marker_sync, container_id, "deny")
        logger.info(f"v2 实例就绪 | name={name} | ip={ip} | work_area={kwargs['work_area_id']}")
        return ContainerInfo(
            container_id=container_id,
            container_name=name,
            container_ip=ip,
            sandbox_type=sandbox_type,
            mounted=False,
            mount_path="/workspace",
            env_injected=bool(kwargs.get("extra_env")),
            network=network,
        )

    async def stop_wa_container(self, container_id: str) -> None:
        """归档前停止全部进程；工作区挂载和容器暂时保留，重试停止幂等。"""
        def stop() -> None:
            try:
                self._docker.containers.get(container_id).stop(timeout=10)
            except docker.errors.NotFound:
                return
        await asyncio.to_thread(stop)

    async def switch_wa_network(self, info: ContainerInfo, mode: NetworkMode) -> ContainerInfo:
        """v2 实例按策略在两张 v2 网之间热切换（语义同 switch_network）。"""
        target = settings.wa_network_for_mode(mode)
        if info.network == target:
            return info
        old_ip = info.container_ip
        new_ip = await asyncio.to_thread(self._switch_network_sync, info.container_id, target)
        if not await self.wait_healthy(new_ip, timeout=10):
            raise RuntimeError(f"切网后 v2 实例不可达 | name={info.container_name} | ip={new_ip}")
        await asyncio.to_thread(self._write_policy_marker_sync, info.container_id, mode)
        info.container_ip = new_ip
        info.network = target
        logger.info(f"v2 实例已切网 | name={info.container_name} | mode={mode} | ip={old_ip}->{new_ip}")
        return info
