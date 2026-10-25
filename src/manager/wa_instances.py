"""
v2 执行实例编排（ADR 0025）

一个工作区（work_area_id）最多一个实例。实例 = 无特权容器 + bind 列表：

    /workspace/work           rw  工作区（宿主目录，实例回收后保留）
    /workspace/deliverables   rw  工作区交付目录（平台在归档点读取）
    /workspace/inputs         ro  授权给本工作区 / 数字人的用户文件（硬链接视图）
    /workspace/context        ro  平台生成的说明文档
    /workspace/role/<name>    ro|rw  角色空间目录（cr-fs rclone 挂载，rslave）

分配：自检不过 → 503；同工作区已有实例 → 体检后复用（形状不同且空闲则重建，忙则 409）；
否则按每数字人 / 每用户上限占名额（超限 409 附占用者）后创建。释放即销毁容器，工作区保留。
Hub 重启后从容器标签与 env 恢复在册记录与挂载引用。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger

from src.config import settings
from src.manager.container_manager import Bind, ContainerManager
from src.manager.fs_sidecar import FsSidecar, RoleMounts
from src.manager.inputs_cache import InputsCache, InputsError, validate_inputs
from src.manager.netguard import NetGuard, SelfTest
from src.manager.paths import valid_id, valid_mount_name, valid_prefix
from src.manager.wa_jobs import JOB_ORIGIN_HEADER, JobLedger, iso_utc, parse_report
from src.manager.wa_registry import LimitExceeded, WorkAreaRegistry
from src.manager.work_areas import InstanceDirs, WorkAreas
from src.models import (
    MOUNT_LAYOUT_LABEL,
    ROLE_LABEL,
    SANDBOX_ID_LABEL,
    SESSION_LABEL,
    USER_LABEL,
    WORK_AREA_LABEL,
    ContainerInfo,
    InputFileSpec,
    ManagedContainer,
    NetworkMode,
    RoleMountSpec,
    SandboxType,
    WorkAreaInstance,
)
from src.proxy.forwarder import SANDBOX_TOKEN_HEADER, close_client

LAYOUT_VERSION = 1
CWD = "/workspace/work"
# 这些原因下实例 API 已不可达，销毁前不再做最后一次任务观测（徒增超时）
_UNREACHABLE_REASONS = frozenset({"container_gone", "unhealthy", "create_failed"})


class WorkAreaError(Exception):
    """v2 业务错误：``status`` = HTTP 状态码，``reason`` = 机器可读原因。"""

    def __init__(self, status: int, reason: str, message: str, **extra) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.message = message
        self.extra = extra

    def detail(self) -> dict:
        return {"reason": self.reason, "message": self.message, **self.extra}


@dataclass
class AcquireSpec:
    work_area_id: str
    user_id: str
    role_id: str
    sandbox_type: SandboxType = "code"
    session_id: str = ""
    turn_id: str = ""
    mounts: list[RoleMountSpec] = field(default_factory=list)
    inputs: Optional[list[InputFileSpec]] = None
    context: Optional[dict[str, str]] = None
    env: Optional[dict[str, str]] = None
    network_mode: NetworkMode = "allow"
    limits: dict = field(default_factory=dict)


def validate_spec(spec: AcquireSpec) -> None:
    for value, what in (
        (spec.work_area_id, "work_area_id"),
        (spec.user_id, "user_id"),
        (spec.role_id, "role_id"),
    ):
        if not valid_id(value):
            raise WorkAreaError(400, "invalid_request", f"{what} 非法：{value!r}")
    names: set[str] = set()
    for mount in spec.mounts:
        if not valid_mount_name(mount.name) or mount.name in names:
            raise WorkAreaError(
                400, "invalid_mount", f"角色目录名非法或重复：{mount.name!r}"
            )
        if not mount.bucket or not valid_prefix(mount.prefix):
            raise WorkAreaError(
                400, "invalid_mount", f"角色目录前缀非法：{mount.bucket}/{mount.prefix}"
            )
        expected = f"roles/{spec.role_id}/"
        if not mount.prefix.strip("/").startswith(expected):
            raise WorkAreaError(
                400, "invalid_mount", f"角色目录须位于 {expected} 之下：{mount.prefix}"
            )
        names.add(mount.name)
    if spec.inputs is not None:
        try:
            validate_inputs(spec.inputs)
        except ValueError as exc:
            raise WorkAreaError(400, "invalid_inputs", str(exc)) from exc


class WorkAreaService:
    def __init__(
        self,
        manager: ContainerManager,
        sidecar: FsSidecar,
        registry: Optional[WorkAreaRegistry] = None,
        inputs: Optional[InputsCache] = None,
    ) -> None:
        self._manager = manager
        self._fs = sidecar
        self.registry = registry or WorkAreaRegistry()
        self.mounts = RoleMounts(sidecar)
        self.work_areas = WorkAreas(sidecar)
        self.dirs = InstanceDirs(sidecar)
        self.inputs = inputs or InputsCache(sidecar.root)
        self.netguard = NetGuard(sidecar, manager)
        self.selftest = SelfTest(sidecar, self.netguard, manager)
        self.jobs = JobLedger(sidecar.host_path("job-ledger"))
        # 正在销毁的实例（sandbox_id → work_area_id）：任务尚未结算，/jobs 期间按来源未知
        self._discarding: dict[str, str] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._deleting: set[str] = set()
        self._last_selftest = 0.0
        self.started = False

    # ── 启动 / 收养 ─────────────────────────────────────────────────────────

    async def startup(self) -> None:
        """确保 v2 网络、网关、cr-fs、netguard；收养 Hub 重启前的 v2 实例。失败不阻断 v1。"""
        if not settings.WORK_AREA_ENABLED:
            logger.info("work_area.enabled=false：v2 不启用")
            return
        try:
            await self._manager.ensure_wa_networks()
            await self._manager.ensure_wa_gateway()
            mounts_lost = await self._fs.ensure()
            await self.netguard.apply()
        except Exception as exc:
            logger.error(
                f"v2 地基准备失败（v2 分配将返回 503，v1 不受影响）| err={exc}"
            )
            return
        managed = [c for c in await self._manager.list_managed() if c.is_v2]
        await self._adopt(managed, mounts_lost=mounts_lost)
        await self._sweep_instance_dirs()
        self.started = True

    @staticmethod
    def _instance_from_labels(c: ManagedContainer) -> Optional[WorkAreaInstance]:
        labels = c.labels
        try:
            layout = json.loads(labels.get(MOUNT_LAYOUT_LABEL) or "[]")
            mounts = tuple(
                RoleMountSpec(
                    name=m["name"],
                    bucket=m["bucket"],
                    prefix=m["prefix"],
                    mode=m["mode"],
                )
                for m in layout
            )
        except (ValueError, KeyError, TypeError):
            return None
        sandbox_id = labels.get(SANDBOX_ID_LABEL, "")
        work_area_id = labels.get(WORK_AREA_LABEL, "")
        if not (
            valid_id(sandbox_id)
            and valid_id(work_area_id)
            and c.api_token
            and c.container_ip
        ):
            return None
        network_mode: NetworkMode = (
            "deny" if c.network == settings.wa_network_isolated else "allow"
        )
        return WorkAreaInstance(
            sandbox_id=sandbox_id,
            work_area_id=work_area_id,
            user_id=labels.get(USER_LABEL, ""),
            role_id=labels.get(ROLE_LABEL, ""),
            sandbox_type=c.sandbox_type,  # type: ignore[arg-type]
            api_token=c.api_token,
            status="ready",
            container_info=ContainerInfo(
                container_id=c.container_id,
                container_name=c.container_name,
                container_ip=c.container_ip,
                sandbox_type=c.sandbox_type,  # type: ignore[arg-type]
                env_injected=c.env_injected,
                network=c.network,
            ),
            mounts=mounts,
            session_id=labels.get(SESSION_LABEL, ""),
            network_mode=network_mode,
        )

    async def _adopt(
        self, managed: list[ManagedContainer], *, mounts_lost: bool
    ) -> None:
        status = {} if mounts_lost else await self._fs.mount_status()
        adopted = 0
        for c in managed:
            instance = self._instance_from_labels(c) if c.status == "running" else None
            if instance is not None:
                await asyncio.to_thread(self._restore_origin, instance)
            reason = ""
            if instance is None:
                reason = f"status={c.status} 或标签不完整"
            elif mounts_lost or not all(status.get(m.key) for m in instance.mounts):
                reason = "角色目录挂载已丢失"
            elif not await self._manager.is_healthy(c.container_ip):
                reason = "API 不可达"
            elif not await self.registry.adopt(instance):
                reason = "同工作区已有在册实例"
            if reason:
                logger.info(
                    f"清理遗留 v2 实例 | name={c.container_name} | reason={reason}"
                )
                await self._manager.remove_container(c.container_id)
                sandbox_id = c.labels.get(SANDBOX_ID_LABEL, "")
                if valid_id(sandbox_id):
                    self.dirs.remove(sandbox_id)
                    work_area_id = c.labels.get(WORK_AREA_LABEL, "")
                    if valid_id(work_area_id):
                        self.jobs.settle_sandbox(work_area_id, sandbox_id, time.time())
                continue
            self.mounts.restore(instance.mounts)
            instance.mounts_held = True
            adopted += 1
        if adopted:
            logger.info(f"启动恢复：收养 {adopted} 个 v2 实例")

    async def _sweep_instance_dirs(self) -> None:
        tracked = {r.sandbox_id for r in self.registry.list_all()}
        for sandbox_id in self.dirs.list_ids():
            if sandbox_id not in tracked:
                self.dirs.remove(sandbox_id)

    # ── 能力 ────────────────────────────────────────────────────────────────

    def capabilities(self) -> dict:
        data = self.selftest.result.to_dict()
        data.update(
            {
                "layout_version": LAYOUT_VERSION,
                "cwd": CWD,
                "limits": {
                    "max_per_role": settings.WORK_AREA_MAX_PER_ROLE,
                    "max_per_user": settings.WORK_AREA_MAX_PER_USER,
                    "job_max_lifetime": settings.WORK_AREA_JOB_MAX_LIFETIME,
                    "idle_backstop_ttl": settings.WORK_AREA_IDLE_BACKSTOP_TTL,
                    "idle_release_seconds": settings.WORK_AREA_IDLE_RELEASE_SECONDS,
                    "soft_cap_bytes": settings.WORK_AREA_SOFT_CAP_BYTES,
                },
                "instances": len(self.registry.list_all()),
            }
        )
        return data

    async def run_selftest(self) -> dict:
        self._last_selftest = time.monotonic()
        await self.selftest.run()
        return self.capabilities()

    def effective_report(self, instance: WorkAreaInstance) -> dict:
        """实际生效的布局与隔离：平台逐项校验，任一项不符即报错、不回退。"""
        info = instance.container_info
        manifest = self.dirs.path(instance.sandbox_id) / "inputs.json"
        try:
            input_files = len(json.loads(manifest.read_text()))
        except (FileNotFoundError, ValueError):
            input_files = 0
        context_dir = self.dirs.context_dir(instance.sandbox_id)
        context_files = (
            sorted(
                p.relative_to(context_dir).as_posix()
                for p in context_dir.rglob("*")
                if p.is_file()
            )
            if context_dir.is_dir()
            else []
        )
        return {
            "layout_version": LAYOUT_VERSION,
            "cwd": CWD,
            "mounts": [
                {"target": "/workspace", "mode": "ro", "source": "platform"},
                {"target": "/workspace/work", "mode": "rw", "source": "work_area"},
                {
                    "target": "/workspace/deliverables",
                    "mode": "rw",
                    "source": "work_area",
                },
                {"target": "/workspace/inputs", "mode": "ro", "source": "inputs"},
                {"target": "/workspace/context", "mode": "ro", "source": "context"},
                *[
                    {
                        "target": m.target,
                        "mode": m.mode,
                        "source": f"role:{m.name}",
                        "bucket": m.bucket,
                        "prefix": m.prefix,
                    }
                    for m in instance.mounts
                ],
            ],
            "inputs": {"files": input_files},
            "context": {"files": context_files},
            "isolation": {
                "privileged": False,
                "fuse_device": False,
                "storage_credentials": False,
                "icc": False,
                "netguard": self.selftest.result.items.get("netguard_rules") is not None
                and self.selftest.result.items["netguard_rules"].ok,
                "api_token": True,
                "network": info.network if info else "",
                "network_mode": instance.network_mode,
                "checked_at": self.selftest.result.checked_at.isoformat()
                if self.selftest.result.checked_at
                else None,
            },
        }

    def _require_safe(self, sandbox_type: str) -> None:
        failing = self.selftest.result.failing(sandbox_type)
        if failing:
            raise WorkAreaError(
                503,
                "isolation_unavailable",
                "v2 隔离前提不满足，拒绝分配（不回退到 v1）",
                failing=failing,
                capabilities=self.selftest.result.to_dict()["items"],
            )

    # ── 分配 ────────────────────────────────────────────────────────────────

    async def acquire(self, spec: AcquireSpec) -> tuple[WorkAreaInstance, bool]:
        """返回 (实例, 是否复用)。"""
        validate_spec(spec)
        self._require_safe(spec.sandbox_type)
        if spec.work_area_id in self._deleting:
            raise WorkAreaError(409, "work_area_deleting", "工作区正在删除")
        async with self._locks[spec.work_area_id]:
            if spec.work_area_id in self._deleting:
                raise WorkAreaError(409, "work_area_deleting", "工作区正在删除")
            existing = self.registry.find_by_work_area(spec.work_area_id)
            if existing is not None:
                reused = await self._try_reuse(existing, spec)
                if reused is not None:
                    return reused, True
            instance = WorkAreaInstance(
                sandbox_id=f"sb_{uuid.uuid4().hex[:16]}",
                work_area_id=spec.work_area_id,
                user_id=spec.user_id,
                role_id=spec.role_id,
                sandbox_type=spec.sandbox_type,
                api_token=secrets.token_urlsafe(32),
                mounts=tuple(spec.mounts),
                session_id=spec.session_id,
                turn_id=spec.turn_id,
                network_mode=spec.network_mode,
            )
            try:
                await self.registry.reserve(
                    instance,
                    max_per_role=settings.WORK_AREA_MAX_PER_ROLE,
                    max_per_user=settings.WORK_AREA_MAX_PER_USER,
                )
            except LimitExceeded as exc:
                raise WorkAreaError(
                    409,
                    f"{exc.scope}_limit",
                    f"{'数字人' if exc.scope == 'role' else '用户'}同时使用电脑的实例已达上限 {exc.limit}",
                    limit=exc.limit,
                    holders=[h.holder() for h in exc.holders],
                ) from exc
            try:
                await self._create(instance, spec)
            except WorkAreaError:
                await self._discard(instance, "create_failed")
                raise
            except Exception as exc:
                await self._discard(instance, "create_failed")
                logger.error(
                    f"v2 实例创建失败 | work_area={spec.work_area_id} | err={exc}"
                )
                raise WorkAreaError(
                    503, "instance_start_failed", f"实例创建失败：{exc}"
                ) from exc
            return instance, False

    async def _try_reuse(
        self, existing: WorkAreaInstance, spec: AcquireSpec
    ) -> Optional[WorkAreaInstance]:
        """可复用则就地更新并返回；需重建则销毁旧实例返回 None；忙则 409。"""
        if existing.user_id != spec.user_id or existing.role_id != spec.role_id:
            raise WorkAreaError(
                409,
                "work_area_owner_mismatch",
                "工作区属于其他用户或数字人",
                holders=[existing.holder()],
            )
        info = existing.container_info
        healthy = (
            existing.status == "ready"
            and info is not None
            and await self._manager.is_healthy(info.container_ip)
        )
        same_shape = (
            existing.sandbox_type == spec.sandbox_type
            and existing.mount_keys == tuple(sorted(m.key for m in spec.mounts))
            and {m.target for m in existing.mounts} == {m.target for m in spec.mounts}
        )
        if healthy and same_shape:
            try:
                await self._ensure_mounts_alive(existing)
            except Exception as exc:
                # 角色目录已坏，实例里的文件读写本就失败，重建是唯一出路
                logger.warning(
                    f"复用 v2 实例失败，重建 | id={existing.sandbox_id} | err={exc}"
                )
                await self._discard(existing, "mounts_dead")
                return None
            # 以下失败不销毁实例（里面可能有在跑的任务）：报错交给调用方重试
            try:
                await self._apply_network(existing, spec.network_mode)
            except Exception as exc:
                raise WorkAreaError(
                    503, "network_switch_failed", f"网络策略切换失败：{exc}"
                ) from exc
            await self._apply_views(existing, spec.inputs, spec.context)
            await asyncio.to_thread(
                self._save_origin,
                existing,
                spec.session_id or existing.session_id,
                spec.turn_id,
            )
            existing.session_id = spec.session_id or existing.session_id
            existing.turn_id = spec.turn_id
            self.registry.touch(existing.sandbox_id)
            return existing
        if healthy:
            activity = await self.activity(existing)
            if not activity.get("idle"):
                raise WorkAreaError(
                    409,
                    "instance_busy",
                    "工作区实例正在运行任务，且本次请求的镜像或角色目录与之不同",
                    holders=[dict(existing.holder(), activity=activity)],
                )
            reason = "shape_changed"
        else:
            reason = "unhealthy"
        logger.info(f"v2 实例重建 | id={existing.sandbox_id} | reason={reason}")
        await self._discard(existing, reason)
        return None

    async def _ensure_mounts_alive(self, instance: WorkAreaInstance) -> None:
        if not instance.mounts:
            return
        status = await self._fs.mount_status()
        dead = [m.name for m in instance.mounts if not status.get(m.key)]
        if dead:
            raise RuntimeError(f"角色目录挂载已失效：{dead}")

    async def _create(self, instance: WorkAreaInstance, spec: AcquireSpec) -> None:
        await self.work_areas.ensure(instance.work_area_id)
        self.dirs.create(instance.sandbox_id)
        await asyncio.to_thread(
            self._save_origin, instance, spec.session_id, spec.turn_id
        )
        await self.mounts.acquire(instance.mounts)
        instance.mounts_held = True
        await self._apply_views(instance, spec.inputs or [], spec.context or {})
        sources = self.work_areas.bind_sources(instance.work_area_id)
        binds = [
            Bind(str(self.dirs.root_dir(instance.sandbox_id)), "/workspace", True),
            Bind(str(sources["work"]), "/workspace/work", False),
            Bind(str(sources["deliverables"]), "/workspace/deliverables", False),
            Bind(
                str(self.dirs.inputs_dir(instance.sandbox_id)),
                "/workspace/inputs",
                True,
            ),
            Bind(
                str(self.dirs.context_dir(instance.sandbox_id)),
                "/workspace/context",
                True,
            ),
            *[
                Bind(
                    str(self.mounts.host_path(m.key)),
                    m.target,
                    m.mode == "ro",
                    "rslave",
                )
                for m in instance.mounts
            ],
        ]
        layout = [
            {"name": m.name, "bucket": m.bucket, "prefix": m.prefix, "mode": m.mode}
            for m in instance.mounts
        ]
        info = await self._manager.run_wa_container(
            sandbox_id=instance.sandbox_id,
            work_area_id=instance.work_area_id,
            user_id=instance.user_id,
            role_id=instance.role_id,
            session_id=instance.session_id,
            sandbox_type=instance.sandbox_type,
            api_token=instance.api_token,
            binds=binds,
            mount_layout=layout,
            extra_env=spec.env or None,
            network=settings.wa_network_for_mode(spec.network_mode),
            limits=spec.limits or {},
        )
        instance.container_info = info
        instance.status = "ready"
        self.registry.touch(instance.sandbox_id)
        logger.info(
            f"v2 实例已分配 | id={instance.sandbox_id} | work_area={instance.work_area_id} "
            f"| user={instance.user_id} | role={instance.role_id} | mounts={[m.name for m in instance.mounts]}"
        )

    def _save_origin(
        self, instance: WorkAreaInstance, session_id: str, turn_id: str
    ) -> None:
        # Docker 标签不能在复用时改写；宿主记录保证重启后仍能恢复真实最近使用者。
        path = self.dirs.path(instance.sandbox_id) / "origin.json"
        temporary = path.with_suffix(".tmp")
        value = dict(
            schema_version=1,
            work_area_id=instance.work_area_id,
            user_id=instance.user_id,
            role_id=instance.role_id,
            session_id=session_id,
            turn_id=turn_id,
        )
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)

    def _restore_origin(self, instance: WorkAreaInstance) -> None:
        path = self.dirs.path(instance.sandbox_id) / "origin.json"
        instance.session_id = instance.turn_id = ""
        try:
            value = json.loads(path.read_text())
            if (
                value.get("schema_version") != 1
                or value.get("work_area_id") != instance.work_area_id
                or value.get("user_id") != instance.user_id
                or value.get("role_id") != instance.role_id
                or not isinstance(value.get("session_id"), str)
                or not isinstance(value.get("turn_id"), str)
            ):
                raise ValueError("执行来源与实例身份不一致")
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, AttributeError):
            logger.warning(
                "执行来源无法恢复，保留工作区但不猜测回合 sandbox={}",
                instance.sandbox_id,
            )
            return
        instance.session_id, instance.turn_id = value["session_id"], value["turn_id"]

    async def apply_network_all(
        self,
        mode: NetworkMode,
        *,
        user_id: Optional[str] = None,
        role_id: Optional[str] = None,
    ) -> tuple[list[dict], list[dict]]:
        """业务侧网络开关热改：把在册实例立即切到 ``mode``，返回 (已切换, 失败)。"""
        switched: list[dict] = []
        failed: list[dict] = []
        for instance in self.registry.list_all():
            if user_id and instance.user_id != user_id:
                continue
            if role_id and instance.role_id != role_id:
                continue
            async with self._locks[instance.work_area_id]:
                if self.registry.get(instance.sandbox_id) is not instance:
                    continue
                if instance.container_info is None or instance.network_mode == mode:
                    continue
                try:
                    await self._apply_network(instance, mode)
                except Exception as exc:
                    logger.warning(
                        f"v2 实例切网失败 | id={instance.sandbox_id} | err={exc}"
                    )
                    failed.append(
                        {
                            "sandbox_id": instance.sandbox_id,
                            "error": "network switch failed",
                        }
                    )
                    continue
            switched.append(
                {
                    "sandbox_id": instance.sandbox_id,
                    "work_area_id": instance.work_area_id,
                }
            )
        return switched, failed

    async def _apply_network(
        self, instance: WorkAreaInstance, mode: NetworkMode
    ) -> None:
        info = instance.container_info
        if info is None:
            return
        old_ip = info.container_ip
        await self._manager.switch_wa_network(info, mode)
        instance.network_mode = mode
        if info.container_ip != old_ip:
            try:
                await close_client(old_ip)
            except Exception:
                pass

    async def _apply_views(
        self,
        instance: WorkAreaInstance,
        inputs: Optional[list[InputFileSpec]],
        context: Optional[dict[str, str]],
    ) -> None:
        """None = 不变；空列表 / 空字典 = 清空。"""
        if inputs is not None:
            await self._materialize(instance, inputs)
        if context is not None:
            await self._write_context(instance, context)

    async def _materialize(
        self, instance: WorkAreaInstance, files: list[InputFileSpec]
    ) -> dict:
        try:
            return await self.inputs.materialize(
                self.dirs.path(instance.sandbox_id), instance.user_id, files
            )
        except ValueError as exc:
            raise WorkAreaError(400, "invalid_inputs", str(exc)) from exc
        except InputsError as exc:
            raise WorkAreaError(422, "inputs_unavailable", str(exc)) from exc

    async def _write_context(
        self, instance: WorkAreaInstance, files: dict[str, str]
    ) -> list[str]:
        try:
            return await asyncio.to_thread(
                self.dirs.write_context, instance.sandbox_id, files
            )
        except ValueError as exc:
            raise WorkAreaError(400, "invalid_context", str(exc)) from exc

    # ── 释放 / 更新 ─────────────────────────────────────────────────────────

    def get(
        self, sandbox_id: str, *, include_quiesced: bool = False
    ) -> WorkAreaInstance:
        instance = self.registry.get(sandbox_id)
        if instance is None or instance.status not in (
            {"ready", "quiesced"} if include_quiesced else {"ready"}
        ):
            raise WorkAreaError(404, "sandbox_not_found", f"实例 {sandbox_id!r} 不存在")
        return instance

    async def release(
        self, sandbox_id: str, reason: str = "release"
    ) -> WorkAreaInstance:
        instance = self.get(sandbox_id, include_quiesced=True)
        async with self._locks[instance.work_area_id]:
            current = self.registry.get(sandbox_id)
            if current is None:
                raise WorkAreaError(
                    404, "sandbox_not_found", f"实例 {sandbox_id!r} 不存在"
                )
            await self._discard(current, reason)
            return current

    async def quiesce(self, sandbox_id: str) -> None:
        """停止租户全部进程，保留挂载和登记，让平台随后归档并释放。"""
        instance = self.get(sandbox_id, include_quiesced=True)
        async with self._locks[instance.work_area_id]:
            current = self.get(sandbox_id, include_quiesced=True)
            if current.container_info is not None:
                if current.status == "ready":
                    await self.activity(current)  # 停止前最后一次任务观测
                await self._manager.stop_wa_container(
                    current.container_info.container_id
                )
                # 进程已全部停止：仍未结束的任务最迟在此刻结束
                self.jobs.settle_sandbox(current.work_area_id, sandbox_id, time.time())
                # 归档失败后停止实例仍在登记；下一次申请必须重建，不能报告复用成功。
                current.status = "quiesced"

    async def set_files_cutover(self, enabled: bool) -> None:
        """排空窗口内持久化切换状态并重建网关；失败保留排空，交由发布流程回滚。"""
        marker = Path(settings.WORK_AREA_ROOT) / ".files-cutover.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        if enabled:
            temporary = marker.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "since": datetime.now(timezone.utc).isoformat(),
                    }
                )
            )
            temporary.replace(marker)
        else:
            marker.unlink(missing_ok=True)
        await self._manager.ensure_gateway()
        await self._manager.ensure_wa_gateway()

    async def update_inputs(self, sandbox_id: str, files: list[InputFileSpec]) -> dict:
        instance = self.get(sandbox_id)
        async with self._locks[instance.work_area_id]:
            result = await self._materialize(self.get(sandbox_id), files)
        self.registry.touch(sandbox_id)
        return result

    async def update_context(self, sandbox_id: str, files: dict[str, str]) -> list[str]:
        instance = self.get(sandbox_id)
        async with self._locks[instance.work_area_id]:
            written = await self._write_context(self.get(sandbox_id), files)
        self.registry.touch(sandbox_id)
        return written

    async def _probe_terminal(self, instance: WorkAreaInstance) -> dict:
        info = instance.container_info
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.get(
                f"http://{info.container_ip}:{settings.SANDBOX_API_PORT}/api/terminal/activity",
                headers={SANDBOX_TOKEN_HEADER: instance.api_token},
            )
            response.raise_for_status()
            return response.json()

    async def activity(self, instance: WorkAreaInstance) -> dict:
        """容器内终端活性；不刷新 last_active_at（否则兜底 TTL 永远不触发）。

        同时是一次任务观测：容器回报的任务清单并入工作区任务台账，``jobs_known`` 表示
        本次观测是否拿到了完整清单（旧镜像不回报清单时为 False）。
        """
        info = instance.container_info
        if info is None:
            return {"known": False, "idle": False, "jobs_known": False, "error": "instance starting"}
        token = self.jobs.observation_token(instance.sandbox_id)
        try:
            data = await self._probe_terminal(instance)
            counts = [
                data.get("running_jobs"),
                data.get("live_panes"),
                data.get("marked_processes"),
            ]
            if any(type(v) is not int or v < 0 for v in counts):
                raise ValueError("invalid activity counters")
        except Exception as exc:
            return {"known": False, "idle": False, "jobs_known": False, "error": str(exc)}
        try:
            report = parse_report(data)
        except ValueError as exc:
            logger.warning(f"实例任务清单无效 | id={instance.sandbox_id} | err={exc}")
            report = None
        if report is not None:
            self.jobs.merge(instance.work_area_id, instance.sandbox_id, report, time.time(), token)
        # 运行中的长驻进程（引擎）已计入 running_jobs；单列出来供平台区分。旧镜像没有该字段 → None
        procs = data.get("running_procs")
        return {
            "known": True,
            "idle": all(v == 0 for v in counts),
            "running_jobs": counts[0],
            "running_procs": procs if type(procs) is int and procs >= 0 else None,
            "live_panes": counts[1],
            "marked_processes": counts[2],
            "last_active_at": instance.last_active_at.isoformat(),
            "jobs_known": report is not None,
        }

    def origin_header(self, instance: WorkAreaInstance) -> dict[str, str]:
        """经 proxy 启动终端任务时交给容器的来源（请求时实例当前的会话与回合）。"""
        value = json.dumps(
            {"session_id": instance.session_id, "turn_id": instance.turn_id},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return {JOB_ORIGIN_HEADER: value}

    @contextlib.asynccontextmanager
    async def terminal_start(self, instance: WorkAreaInstance):
        """包住一次经 proxy 的终端任务启动请求：请求开始前落盘「有未观测启动」。"""
        self.jobs.start_begin(instance.work_area_id, instance.sandbox_id, time.time())
        try:
            yield
        finally:
            self.jobs.start_end(instance.sandbox_id)

    async def job_report(self, work_area_id: str) -> dict:
        """``GET /v2/work-areas/{id}/jobs``：工作区任务来源清单（契约见 createrole_com_api#38 C.2）。"""
        if not valid_id(work_area_id):
            raise WorkAreaError(400, "invalid_request", f"work_area_id 非法：{work_area_id!r}")
        instance = self.registry.find_by_work_area(work_area_id)
        if (
            instance is None
            and not self.work_areas.exists(work_area_id)
            and not self.jobs.exists(work_area_id)
        ):
            raise WorkAreaError(404, "work_area_not_found", f"工作区 {work_area_id!r} 不存在")
        live = {s for s, wa in self._discarding.items() if wa == work_area_id}
        observed = not live  # 正在销毁：任务尚未结算
        if instance is not None:
            live.add(instance.sandbox_id)
            if instance.status == "ready":
                report = await self.activity(instance)
                observed = observed and report.get("jobs_known") is True
        now = time.time()
        jobs, complete = self.jobs.snapshot(work_area_id, live, now)
        return {"known": observed and complete, "now": iso_utc(now), "jobs": jobs}

    async def delete_work_area(self, work_area_id: str, *, force: bool = False) -> dict:
        if not valid_id(work_area_id):
            raise WorkAreaError(
                400, "invalid_request", f"work_area_id 非法：{work_area_id!r}"
            )
        if work_area_id in self._deleting:
            raise WorkAreaError(409, "work_area_deleting", "工作区正在删除")
        self._deleting.add(work_area_id)
        try:
            async with self._locks[work_area_id]:
                instance = self.registry.find_by_work_area(work_area_id)
                if instance is not None:
                    if not force:
                        raise WorkAreaError(
                            409,
                            "work_area_in_use",
                            "工作区仍有实例，先释放或带 force=true",
                            holders=[instance.holder()],
                        )
                    await self._discard(instance, "work_area_deleted")
                result = await self.work_areas.remove(work_area_id)
                self.jobs.remove(work_area_id)
                return result
        finally:
            self._deleting.discard(work_area_id)

    def is_deleting(self, work_area_id: str) -> bool:
        return work_area_id in self._deleting

    async def _discard(self, instance: WorkAreaInstance, reason: str) -> None:
        """摘册 + 销毁容器 + 释放挂载引用 + 删实例目录（工作区不动）。

        销毁前尽力做最后一次任务观测；销毁后仍未结束的任务按销毁时刻结束。
        """
        self._discarding[instance.sandbox_id] = instance.work_area_id
        try:
            await self.registry.remove(instance.sandbox_id)
            info = instance.container_info
            if info is not None:
                if instance.status == "ready" and reason not in _UNREACHABLE_REASONS:
                    await self.activity(instance)
                try:
                    await close_client(info.container_ip)
                except Exception:
                    pass
                await self._manager.remove_container(info.container_id)
            self.jobs.settle_sandbox(instance.work_area_id, instance.sandbox_id, time.time())
        finally:
            self._discarding.pop(instance.sandbox_id, None)
        if instance.mounts_held:
            instance.mounts_held = False
            await self.mounts.release(instance.mount_keys)
        self.dirs.remove(instance.sandbox_id)
        logger.info(
            f"v2 实例已销毁 | id={instance.sandbox_id} | work_area={instance.work_area_id} | reason={reason}"
        )

    # ── 对账 ────────────────────────────────────────────────────────────────

    async def reconcile_once(self) -> None:
        if not settings.WORK_AREA_ENABLED:
            return
        if not self.started:
            await self.startup()
            if not self.started:
                return
        if await self._fs.ensure():
            logger.warning("cr-fs 已重建或重启，角色目录挂载全部失效：销毁全部 v2 实例")
            for instance in self.registry.list_all():
                await self._discard(instance, "fs_sidecar_restarted")
            self.mounts.reset()
        try:
            await self._manager.ensure_wa_gateway()
            await self.netguard.apply()
        except Exception as exc:
            logger.warning(f"v2 网关 / netguard 保活失败 | err={exc}")

        managed = [c for c in await self._manager.list_managed() if c.is_v2]
        now = datetime.now(tz=timezone.utc)
        running = {c.container_id: c for c in managed if c.status == "running"}
        for c in managed:
            if c.status != "running":
                await self._manager.remove_container(c.container_id)
        for instance in self.registry.list_ready():
            info = instance.container_info
            actual = running.get(info.container_id) if info else None
            if actual is None:
                await self._discard(instance, "container_gone")
                continue
            if actual.container_ip and actual.container_ip != info.container_ip:
                old_ip = info.container_ip
                info.container_ip = actual.container_ip
                info.network = actual.network or info.network
                try:
                    await close_client(old_ip)
                except Exception:
                    pass
            ttl = settings.WORK_AREA_IDLE_BACKSTOP_TTL
            if ttl > 0 and (now - instance.last_active_at).total_seconds() > ttl:
                await self._discard(instance, "idle_backstop_ttl")
            elif self.jobs.needs_observation(
                instance.work_area_id, instance.sandbox_id, time.time()
            ):
                # 有未观测的启动或未结束的任务：周期观测，缩小实例意外消失时的来源空白
                await self.activity(instance)
        tracked = self.registry.tracked_container_ids()
        starting = {
            r.sandbox_id for r in self.registry.list_all() if r.status == "starting"
        }
        for c in running.values():
            if c.container_id in tracked or c.labels.get(SANDBOX_ID_LABEL) in starting:
                continue
            age = (now - c.created_at).total_seconds() if c.created_at else 0.0
            if age > settings.ORPHAN_GRACE_SECONDS:
                logger.warning(f"销毁不在册的 v2 孤儿实例 | name={c.container_name}")
                await self._manager.remove_container(c.container_id)
        try:
            await self.mounts.sweep(settings.ROLE_MOUNT_UNMOUNT_GRACE)
        except Exception as exc:
            logger.warning(f"角色目录挂载清理失败 | err={exc}")
        await self.inputs.evict()
        if (
            time.monotonic() - self._last_selftest
            >= settings.WORK_AREA_SELFTEST_INTERVAL
        ):
            await self.run_selftest()
