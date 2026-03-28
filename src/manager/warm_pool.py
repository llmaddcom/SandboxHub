"""
预热容器池

职责：维护每种 sandbox_type 的预热容器列表，提供 acquire/release/maintain 接口。
acquire 从池中弹出容器（<100ms），池空时返回 None（由调用方兜底冷启动）。
release 清理容器 workspace 后归还池。
maintain 后台定期补充池到目标大小。
"""
from __future__ import annotations

import asyncio
from collections import defaultdict

from loguru import logger

from src.config import settings
from src.manager.container_manager import ContainerManager
from src.models import ContainerInfo, SandboxType


class WarmPool:
    def __init__(self, container_manager: ContainerManager) -> None:
        self._manager = container_manager
        # sandbox_type → list of available ContainerInfo
        self._pools: dict[str, list[ContainerInfo]] = defaultdict(list)
        self._lock = asyncio.Lock()
        # sandbox types currently being refilled; prevents concurrent _refill double-spawning
        self._refilling: set[str] = set()

    async def acquire(self, sandbox_type: SandboxType) -> ContainerInfo | None:
        """
        从池中弹出一个可用容器。池空时返回 None。
        调用方负责兜底冷启动（直接 run_container）。
        """
        async with self._lock:
            pool = self._pools.get(sandbox_type, [])
            if not pool:
                return None
            return pool.pop(0)

    async def release(self, container_info: ContainerInfo) -> None:
        """
        清理容器 workspace 并重置 bash session，完成后归还到池。
        调用方 await 此方法，清理完成才返回。
        router 层如需非阻塞，应使用 asyncio.create_task(pool.release(...))。
        """
        try:
            await self._manager.clean_and_reset(container_info.container_ip)
            async with self._lock:
                self._pools[container_info.sandbox_type].append(container_info)
            logger.info(f"容器归还 pool | ip={container_info.container_ip} | type={container_info.sandbox_type}")
        except Exception as e:
            logger.warning(f"容器归还 pool 失败，将销毁 | ip={container_info.container_ip} | err={e}")
            try:
                from src.proxy.forwarder import close_client
                await close_client(container_info.container_ip)
            except Exception:
                pass
            await self._manager.remove_container(container_info.container_id)

    async def ensure_pool(self, sandbox_type: SandboxType) -> None:
        """
        触发一次 pool 补充到配置目标大小。
        供外部调用（如 acquire 后补充），内部委托给 _refill。
        """
        target = settings.pool_size_for_type(sandbox_type)
        if target > 0:
            await self._refill(sandbox_type, target)

    async def _refill(self, sandbox_type: SandboxType, target: int) -> None:
        """补充指定类型到目标数量，并发启动缺少的容器数。已在补充中则跳过。"""
        async with self._lock:
            if sandbox_type in self._refilling:
                return
            current = len(self._pools[sandbox_type])
            needed = target - current
            if needed <= 0:
                return
            self._refilling.add(sandbox_type)

        logger.info(f"补充 pool | type={sandbox_type} | needed={needed}")
        try:
            tasks = [
                asyncio.create_task(self._create_warm(sandbox_type, i))
                for i in range(needed)
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            async with self._lock:
                self._refilling.discard(sandbox_type)

    async def _create_warm(self, sandbox_type: SandboxType, slot: int) -> None:
        """创建单个预热容器，成功后加入 pool；失败只记 warning。"""
        try:
            container = await self._manager.run_container(sandbox_type, slot)
            async with self._lock:
                self._pools[sandbox_type].append(container)
            logger.info(f"预热容器就绪 | type={sandbox_type} | ip={container.container_ip}")
        except Exception as e:
            logger.warning(f"预热容器创建失败 | type={sandbox_type} | err={e}")

    async def maintain_loop(self) -> None:
        """
        后台维护循环，每 POOL_MAINTAIN_INTERVAL 秒检查一次。
        在 FastAPI lifespan 中作为 asyncio.Task 启动。
        """
        while True:
            await asyncio.sleep(settings.POOL_MAINTAIN_INTERVAL)
            for sandbox_type in ("ubuntu",):
                target = settings.pool_size_for_type(sandbox_type)
                if target > 0:
                    await self._refill(sandbox_type, target)

    def available_count(self, sandbox_type: str) -> int:
        return len(self._pools.get(sandbox_type, []))

    def status(self) -> dict:
        return {
            st: {"available": len(pool)}
            for st, pool in self._pools.items()
        }
