"""
沙盒注册表

职责：维护 sandbox_id → SandboxRecord 和 (user_id, role_id) → sandbox_id 两层索引。
写操作（register/mark_released）通过 asyncio.Lock 保护；
读操作（get/find_active/list_all）在单线程 asyncio 事件循环下安全，不加锁。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.models import ContainerInfo, SandboxRecord


class SandboxRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, SandboxRecord] = {}
        # (user_id, role_id) → sandbox_id，仅 status=ready 时有效
        self._by_user_role: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        container_info: ContainerInfo,
        user_id: str,
        role_id: str,
    ) -> SandboxRecord:
        """创建新 SandboxRecord，写入双索引，返回 record。"""
        sandbox_id = f"sb_{uuid.uuid4().hex[:12]}"
        record = SandboxRecord(
            sandbox_id=sandbox_id,
            container_info=container_info,
            user_id=user_id,
            role_id=role_id,
            status="ready",
        )
        async with self._lock:
            self._by_id[sandbox_id] = record
            self._by_user_role[(user_id, role_id)] = sandbox_id
        return record

    async def get(self, sandbox_id: str) -> Optional[SandboxRecord]:
        """按 sandbox_id 查询，不存在返回 None。"""
        return self._by_id.get(sandbox_id)

    async def find_active(self, user_id: str, role_id: str) -> Optional[SandboxRecord]:
        """查找 user+role 对应的 ready 状态 sandbox，不存在返回 None。"""
        sandbox_id = self._by_user_role.get((user_id, role_id))
        if not sandbox_id:
            return None
        record = self._by_id.get(sandbox_id)
        if record and record.status == "ready":
            return record
        return None

    async def mark_released(self, sandbox_id: str) -> Optional[ContainerInfo]:
        """
        标记为 released，清除 user_role 索引。
        返回 ContainerInfo 供调用方归还 pool；不存在返回 None。
        released 记录保留一段时间（见 prune_released）：兼作后台清理在途容器的在册凭据，
        防止对账把清理中的容器误判为孤儿。
        """
        async with self._lock:
            record = self._by_id.get(sandbox_id)
            if not record:
                return None
            record.status = "released"
            record.last_active_at = datetime.now(tz=timezone.utc)
            self._by_user_role.pop((record.user_id, record.role_id), None)
            return record.container_info

    async def evict(self, sandbox_id: str) -> Optional[SandboxRecord]:
        """
        彻底移除记录（容器已死/失联/闲置回收时用）。

        与 mark_released 的差别：不保留 released 记录——容器不归还 pool、不需要在册
        宽限，且要让同 user+role 的下一次 acquire 立即走全新分配。
        """
        async with self._lock:
            record = self._by_id.pop(sandbox_id, None)
            if not record:
                return None
            if self._by_user_role.get((record.user_id, record.role_id)) == sandbox_id:
                self._by_user_role.pop((record.user_id, record.role_id), None)
            return record

    def touch(self, sandbox_id: str) -> None:
        """刷新最近使用时间（acquire 复用 / proxy 转发时调用），供闲置回收判定。"""
        record = self._by_id.get(sandbox_id)
        if record is not None:
            record.last_active_at = datetime.now(tz=timezone.utc)

    async def prune_released(self, max_age_seconds: float) -> int:
        """移除 released 超过 max_age_seconds 的记录，返回移除数量（内存修剪）。"""
        cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=max_age_seconds)
        async with self._lock:
            stale = [
                sid
                for sid, r in self._by_id.items()
                if r.status == "released" and r.last_active_at < cutoff
            ]
            for sid in stale:
                self._by_id.pop(sid, None)
        return len(stale)

    def list_all(self) -> list[SandboxRecord]:
        return list(self._by_id.values())

    def list_ready(self) -> list[SandboxRecord]:
        return [r for r in self._by_id.values() if r.status == "ready"]

    def tracked_container_ids(self) -> set[str]:
        """所有在册记录（含 released 宽限期内的）指向的容器 id，供孤儿对账。"""
        return {r.container_info.container_id for r in self._by_id.values()}

    async def drain(self) -> list[ContainerInfo]:
        """Return all tracked ContainerInfos and clear the registry. Called on shutdown."""
        async with self._lock:
            infos = [r.container_info for r in self._by_id.values()]
            self._by_id.clear()
            self._by_user_role.clear()
        return infos
