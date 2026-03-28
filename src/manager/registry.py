"""
沙盒注册表

职责：维护 sandbox_id → SandboxRecord 和 (user_id, role_id) → sandbox_id 两层索引。
所有方法线程安全（asyncio.Lock）。
"""
from __future__ import annotations

import asyncio
import uuid
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
        """
        async with self._lock:
            record = self._by_id.get(sandbox_id)
            if not record:
                return None
            record.status = "released"
            self._by_user_role.pop((record.user_id, record.role_id), None)
            return record.container_info

    def list_all(self) -> list[SandboxRecord]:
        return list(self._by_id.values())
