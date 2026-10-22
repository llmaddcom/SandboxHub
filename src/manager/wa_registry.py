"""
v2 实例注册表：sandbox_id → WorkAreaInstance，work_area_id → sandbox_id（每个工作区最多一个实例）。

与 v1 的 SandboxRegistry 分开：v1 的闲置回收会把实例「归还 warm pool」，绝不能作用到 v2 实例。
名额按 starting + ready 计数；starting 记录先占位，防并发越限。「每数字人」按 (user_id, role_id)
计——同一数字人可以被多个用户使用（v1 也按这对键分配），全局计数会让共享的数字人互相挤占。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from src.models import WorkAreaInstance


class LimitExceeded(Exception):
    def __init__(self, scope: str, limit: int, holders: list[WorkAreaInstance]) -> None:
        super().__init__(f"{scope} 实例数已达上限 {limit}")
        self.scope = scope
        self.limit = limit
        self.holders = holders


class WorkAreaRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, WorkAreaInstance] = {}
        self._by_work_area: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def get(self, sandbox_id: str) -> Optional[WorkAreaInstance]:
        return self._by_id.get(sandbox_id)

    def find_by_work_area(self, work_area_id: str) -> Optional[WorkAreaInstance]:
        sandbox_id = self._by_work_area.get(work_area_id)
        return self._by_id.get(sandbox_id) if sandbox_id else None

    def list_all(self) -> list[WorkAreaInstance]:
        return list(self._by_id.values())

    def list_ready(self) -> list[WorkAreaInstance]:
        return [r for r in self._by_id.values() if r.status == "ready"]

    def tracked_container_ids(self) -> set[str]:
        return {
            r.container_info.container_id for r in self._by_id.values() if r.container_info is not None
        }

    async def reserve(
        self, instance: WorkAreaInstance, *, max_per_role: int, max_per_user: int
    ) -> None:
        """登记 starting 记录占名额；超限抛 LimitExceeded（附占用者）。工作区已有实例抛 ValueError。"""
        async with self._lock:
            if instance.work_area_id in self._by_work_area:
                raise ValueError(f"工作区 {instance.work_area_id} 已有实例")
            same_user = [r for r in self._by_id.values() if r.user_id == instance.user_id]
            same_role = [r for r in same_user if r.role_id == instance.role_id]
            if max_per_role > 0 and len(same_role) >= max_per_role:
                raise LimitExceeded("role", max_per_role, same_role)
            if max_per_user > 0 and len(same_user) >= max_per_user:
                raise LimitExceeded("user", max_per_user, same_user)
            self._by_id[instance.sandbox_id] = instance
            self._by_work_area[instance.work_area_id] = instance.sandbox_id

    async def adopt(self, instance: WorkAreaInstance) -> bool:
        """Hub 重启后从容器标签恢复（不检查名额）；工作区已有在册实例时返回 False。"""
        async with self._lock:
            if instance.work_area_id in self._by_work_area:
                return False
            self._by_id[instance.sandbox_id] = instance
            self._by_work_area[instance.work_area_id] = instance.sandbox_id
            return True

    async def remove(self, sandbox_id: str) -> Optional[WorkAreaInstance]:
        async with self._lock:
            record = self._by_id.pop(sandbox_id, None)
            if record is not None and self._by_work_area.get(record.work_area_id) == sandbox_id:
                self._by_work_area.pop(record.work_area_id, None)
            return record

    def touch(self, sandbox_id: str) -> None:
        record = self._by_id.get(sandbox_id)
        if record is not None:
            record.last_active_at = datetime.now(tz=timezone.utc)
