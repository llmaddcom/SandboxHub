"""
Docker 容器管理器

职责：封装所有 Docker SDK 操作（run/stop/rm/IP/healthcheck/workspace清理）。
不维护任何状态，是纯操作层。供 WarmPool 调用。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Optional

import docker
import docker.errors
import httpx
from loguru import logger

from src.config import settings
from src.models import ContainerInfo, SandboxType


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

    def _build_container_env(self) -> dict:
        proxy = settings.SANDBOX_HTTP_PROXY
        if not proxy:
            return {}
        return {
            "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy,
            "http_proxy": proxy, "https_proxy": proxy,
            "NO_PROXY": "localhost,127.0.0.1,172.16.0.0/12,10.0.0.0/8",
            "no_proxy": "localhost,127.0.0.1,172.16.0.0/12,10.0.0.0/8",
        }

    # ── Docker 操作（同步，供 to_thread 调用） ────────────────────────────────

    def _run_container_sync(self, sandbox_type: SandboxType, name: str) -> tuple[str, str]:
        """
        docker run，等待 IP，返回 (container_id, container_ip)。
        同步方法，在 asyncio.to_thread 中调用。
        """
        image = settings.image_for_type(sandbox_type)
        # 清理同名残留
        try:
            self._docker.containers.get(name).remove(force=True)
        except docker.errors.NotFound:
            pass

        container = self._docker.containers.run(
            image,
            detach=True,
            name=name,
            network=settings.SANDBOX_NETWORK,
            shm_size="2g",
            security_opt=["seccomp=unconfined"],
            dns=["8.8.8.8", "114.114.114.114"],
            environment=self._build_container_env(),
            labels={
                settings.CONTAINER_LABEL: "true",
                "sandboxhub.type": sandbox_type,
            },
        )

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

    def _list_managed_sync(self) -> list[dict]:
        """列出所有 sandboxhub.managed=true 的容器（恢复用）。"""
        try:
            containers = self._docker.containers.list(
                all=True,
                filters={"label": f"{settings.CONTAINER_LABEL}=true"},
            )
            result = []
            for c in containers:
                if c.status != "running":
                    continue
                try:
                    ip = self._get_container_ip(c)
                    result.append({
                        "container_id": c.id,
                        "container_name": c.name,
                        "container_ip": ip,
                        "sandbox_type": c.labels.get("sandboxhub.type", "ubuntu"),
                    })
                except RuntimeError:
                    continue
            return result
        except Exception as e:
            logger.warning(f"列出管理容器失败: {e}")
            return []

    # ── 异步公开接口 ──────────────────────────────────────────────────────────

    async def run_container(self, sandbox_type: SandboxType, slot: int = 0) -> ContainerInfo:
        """
        启动新容器，等待健康检查，返回 ContainerInfo。
        冷启动路径，在 asyncio.to_thread 中执行 Docker 操作。
        """
        name = self._build_warm_name(sandbox_type, slot)
        container_id, ip = await asyncio.to_thread(
            self._run_container_sync, sandbox_type, name
        )
        # 等待 API 就绪
        if not await self.wait_healthy(ip):
            await asyncio.to_thread(self._stop_and_remove_sync, container_id)
            raise RuntimeError(f"容器健康检查超时 | name={name}")
        logger.info(f"容器就绪 | name={name} | ip={ip}")
        return ContainerInfo(
            container_id=container_id,
            container_name=name,
            container_ip=ip,
            sandbox_type=sandbox_type,
        )

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
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if await self.is_healthy(container_ip):
                return True
            await asyncio.sleep(1)
        return False

    async def clean_and_reset(self, container_ip: str) -> None:
        """
        清理 workspace + 重置 bash session（release 后归还 pool 前调用）。
        失败只记 warning，不中断 pool 归还流程。
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
            logger.warning(f"clean_and_reset 失败 | ip={container_ip} | err={e}")

    def recover_running_containers(self) -> list[ContainerInfo]:
        """应用启动时从 Docker 恢复运行中的受管容器。"""
        raw = self._list_managed_sync()
        return [ContainerInfo(**r) for r in raw]
