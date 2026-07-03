"""
沙盒对账器

职责：让「Docker 里实际存在的受管容器」与「registry + warm pool 的在册状态」保持一致，
是 SandboxHub 的兜底层，覆盖三类失控场景：

- startup()：服务或宿主重启后的恢复——已停止的受管容器（宿主重启后全是 exited）
  就地清除；孤儿挂载容器（角色专属、registry 映射已丢）销毁；健康的非挂载容器
  clean_and_reset 成功后才收养回 warm pool（防止上一任租户的文件/会话串台）。
- loop()：周期对账——已死容器从 registry/pool 摘除并清理（下次 acquire 自动重建）；
  不在册的 running 孤儿容器销毁（带创建时间宽限，避免误杀创建中的容器）；闲置超时
  的已分配沙盒自动回收；released 记录修剪。
- evict_if_dead()：proxy 转发失败时的即时体检——容器 API 探测不可达才驱逐销毁，
  让下一次 acquire 立即重建，不必等对账周期；命令超时等「容器还活着」的失败不误伤。

registry/pool 只在册不体检、ContainerManager 只操作不决策，一致性决策集中在本层。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from loguru import logger

from src.config import settings
from src.manager.container_manager import ContainerManager
from src.manager.registry import SandboxRegistry
from src.manager.warm_pool import WarmPool
from src.models import ContainerInfo, ManagedContainer, SandboxRecord
from src.proxy.forwarder import close_client

# released 记录的保留时长（秒）：兼作「后台清理在途容器」的在册宽限，须大于一次
# release 清理（clean_and_reset ≤15s + 归还）的最坏耗时。
_RELEASED_RECORD_TTL = 600


class SandboxReconciler:
    def __init__(
        self,
        registry: SandboxRegistry,
        warm_pool: WarmPool,
        container_manager: ContainerManager,
    ) -> None:
        self._registry = registry
        self._pool = warm_pool
        self._manager = container_manager

    # ── 启动恢复 ─────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        """服务启动时对 Docker 遗留的受管容器做一次性处置（在开始服务前完成）。"""
        managed = await self._manager.list_managed()
        adopted = 0
        for c in managed:
            if c.status != "running":
                logger.info(f"清理已停止的受管容器 | name={c.container_name} | status={c.status}")
                await self._manager.remove_container(c.container_id)
            elif c.mounted:
                logger.info(f"清理孤儿挂载容器 | name={c.container_name}")
                await self._manager.remove_container(c.container_id)
            elif await self._adopt_warm(c):
                adopted += 1
        if adopted:
            logger.info(f"启动恢复：收养 {adopted} 个运行中容器回 warm pool")

    async def _adopt_warm(self, c: ManagedContainer) -> bool:
        """收养一个重启前遗留的非挂载容器：健康且清理复位成功才回 pool，否则销毁。

        遗留容器可能重启前已分配给某用户（分配不改容器本体），clean_and_reset 是
        防跨租户串台的硬前提，失败一律销毁。
        """
        if not c.container_ip or not await self._manager.is_healthy(c.container_ip):
            logger.info(f"遗留容器不健康，销毁 | name={c.container_name}")
            await self._manager.remove_container(c.container_id)
            return False
        try:
            await self._manager.clean_and_reset(c.container_ip)
        except Exception as e:
            logger.warning(f"遗留容器清理复位失败，销毁 | name={c.container_name} | err={e}")
            await self._manager.remove_container(c.container_id)
            return False
        await self._pool.restore(
            ContainerInfo(
                container_id=c.container_id,
                container_name=c.container_name,
                container_ip=c.container_ip,
                sandbox_type=c.sandbox_type,  # type: ignore[arg-type]
            )
        )
        return True

    # ── 周期对账 ─────────────────────────────────────────────────────────────

    async def loop(self) -> None:
        """后台对账循环，每 RECONCILE_INTERVAL 秒一轮；单轮失败不中断循环。"""
        while True:
            await asyncio.sleep(settings.RECONCILE_INTERVAL)
            try:
                await self.reconcile_once()
            except Exception as e:
                logger.warning(f"沙盒对账失败，下一轮重试 | err={e}")

    async def reconcile_once(self) -> None:
        managed = await self._manager.list_managed()
        now = datetime.now(tz=timezone.utc)

        # 1) 已停止的受管容器：从 registry/pool 摘除并删除本体。
        #    被摘除的已分配沙盒，其 user+role 的下一次 acquire 会自动重建。
        for c in managed:
            if c.status == "running":
                continue
            await self._forget(c.container_id)
            await self._manager.remove_container(c.container_id)
            logger.info(f"清理已停止容器 | name={c.container_name} | status={c.status}")

        # 2) 不在册的 running 孤儿容器：超过创建宽限即销毁。宽限覆盖创建在途窗口
        #    （run_container 等健康 + 挂载，尚未进 pool/registry）。
        tracked = self._registry.tracked_container_ids() | self._pool.container_ids()
        for c in managed:
            if c.status != "running" or c.container_id in tracked:
                continue
            age = (now - c.created_at).total_seconds() if c.created_at else 0.0
            if age > settings.ORPHAN_GRACE_SECONDS:
                logger.warning(f"销毁不在册的孤儿容器 | name={c.container_name}")
                await self._destroy(c.container_id, c.container_ip)

        # 3) 在册 ready 但容器已从 Docker 消失（如被手动 docker rm）：驱逐记录。
        managed_running = {c.container_id for c in managed if c.status == "running"}
        for record in self._registry.list_ready():
            if record.container_info.container_id not in managed_running:
                logger.warning(
                    f"沙盒容器已消失，驱逐记录 | id={record.sandbox_id} "
                    f"| name={record.container_info.container_name}"
                )
                await self._evict_record(record)

        # 4) 闲置超时的已分配沙盒：自动回收（挂载容器销毁、warm 容器复位回池）。
        #    调用方（createrole）按 (user, role) 幂等 acquire，回收对其透明。
        if settings.SANDBOX_IDLE_TTL > 0:
            for record in self._registry.list_ready():
                idle = (now - record.last_active_at).total_seconds()
                if idle > settings.SANDBOX_IDLE_TTL:
                    logger.info(
                        f"闲置沙盒自动回收 | id={record.sandbox_id} "
                        f"| user={record.user_id} | idle={int(idle)}s"
                    )
                    evicted = await self._registry.evict(record.sandbox_id)
                    if evicted is not None:
                        await self._pool.release(evicted.container_info)

        # 5) released 记录修剪（内存有界）。
        await self._registry.prune_released(_RELEASED_RECORD_TTL)

    # ── 即时体检（proxy 失败 / acquire 复用时触发） ──────────────────────────

    async def evict_if_dead(self, sandbox_id: str) -> bool:
        """探测沙盒容器 API：不可达则驱逐并销毁，返回是否驱逐。

        proxy 502 后调用。探测兜住误报：命令超时、容器业务错误等场景容器仍可达，
        不会被误杀。
        """
        record = await self._registry.get(sandbox_id)
        if record is None or record.status != "ready":
            return False
        if await self._manager.is_healthy(record.container_info.container_ip):
            return False
        logger.warning(
            f"沙盒失联，驱逐销毁 | id={sandbox_id} "
            f"| ip={record.container_info.container_ip}"
        )
        await self.destroy_sandbox(record)
        return True

    async def destroy_sandbox(self, record: SandboxRecord) -> None:
        """驱逐在册记录并销毁其容器（复用体检失败 / 失联时的统一出口）。"""
        await self._registry.evict(record.sandbox_id)
        await self._destroy(
            record.container_info.container_id, record.container_info.container_ip
        )

    # ── 内部辅助 ─────────────────────────────────────────────────────────────

    async def _forget(self, container_id: str) -> None:
        """把容器从 registry / warm pool 的在册状态中摘除（容器已死，不做清理动作）。"""
        await self._pool.remove_by_id(container_id)
        for record in self._registry.list_ready():
            if record.container_info.container_id == container_id:
                await self._evict_record(record)

    async def _evict_record(self, record: SandboxRecord) -> None:
        await self._registry.evict(record.sandbox_id)
        try:
            await close_client(record.container_info.container_ip)
        except Exception:
            pass

    async def _destroy(self, container_id: str, container_ip: str) -> None:
        if container_ip:
            try:
                await close_client(container_ip)
            except Exception:
                pass
        await self._manager.remove_container(container_id)
