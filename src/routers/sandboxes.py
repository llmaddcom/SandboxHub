# /data/zh/SandboxHub/src/routers/sandboxes.py
"""
沙盒生命周期路由

职责：暴露 acquire/release/status/list/ping 接口。
acquire 优先复用已有 sandbox（复用前体检，容器已死则驱逐重建、对调用方透明自愈），
其次从 warm pool 取（出池体检，死容器销毁换下一个），最后冷启动兜底。
acquire 携带的网络策略（policy.network.default）每次都会落实：容器不在策略对应的网络上
就热切网（运行中 disconnect/connect，进程不重启）；policy/apply 批量对在册沙盒做同样的事。
release 触发后台清理，立即返回 ok。
ping 提供浅检查（registry 状态）和深检查（TCP 可达性）两种健康检查模式。
"""
from __future__ import annotations

import asyncio
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel

from src.config import settings
from src.models import ContainerInfo, NetworkMode, WorkspaceMount
from src.proxy.forwarder import close_client

router = APIRouter(prefix="/v1/sandboxes")

# 依赖注入（app 启动后通过 set_* 注入）
_registry = None
_warm_pool = None
_container_manager = None
_reconciler = None


def set_dependencies(registry, warm_pool, container_manager, reconciler) -> None:
    global _registry, _warm_pool, _container_manager, _reconciler
    _registry = registry
    _warm_pool = warm_pool
    _container_manager = container_manager
    _reconciler = reconciler


# ── 请求/响应模型 ──────────────────────────────────────────────────────────────

class WorkspaceSpec(BaseModel):
    """acquire 可选的工作区挂载诉求：把 MinIO bucket/prefix 实时挂进容器 mount_path。"""
    bucket: str
    prefix: str
    mount_path: str = "/workspace"


class NetworkPolicySpec(BaseModel):
    """网络策略：``default`` 二值；``allow``/``deny`` 域名/CIDR 列表为契约预留（P3 egress 代理），
    本版不支持——非空一律 400 拒绝而非降级（capabilities.network_policy=binary）。"""
    default: NetworkMode = "allow"
    allow: list[str] = []
    deny: list[str] = []


class PolicySpec(BaseModel):
    """acquire / policy.apply 携带的沙盒策略（createrole#449 SandboxPolicy 同构）。

    形状对齐 OpenSandbox / E2B；本版只实现 ``network.default``，``protected_paths`` /
    ``writable_roots`` 原样回显不执行（为去挂载 / 用户机执行器预留）。
    """
    schema_version: int = 1
    network: NetworkPolicySpec = NetworkPolicySpec()
    protected_paths: list[str] = []
    writable_roots: list[str] = []


class EffectivePolicy(BaseModel):
    """实际生效的策略：调用方感知层（离线钩子 / 结果尾注）只吃这个，不吃自己请求的值。
    ``reasons`` 列出与请求的差异或未执行项。"""
    schema_version: int = 1
    network: NetworkPolicySpec
    protected_paths: list[str] = []
    writable_roots: list[str] = []
    reasons: list[str] = []


class AcquireRequest(BaseModel):
    user_id: str
    role_id: str
    sandbox_type: Literal["ubuntu", "code"] = "ubuntu"
    # 可选：携带则请求把该角色云盘挂进容器（见 WorkspaceSpec）。createrole 在开启挂载
    # 开关时随每次 acquire 下发；SandboxHub 缺凭据/总开关关闭时记 warning 并回退为不挂载。
    workspace: Optional[WorkspaceSpec] = None
    # 可选：注入为容器环境变量（issue #15/#16，如 CR_API_BASE / CR_SANDBOX_TOKEN）。
    # 仅容器创建时生效：复用同 (user, role) 已有沙盒时忽略（调用方 token 滑动续期，
    # 首次注入的值持续有效）；首次分配且非空时绕过 warm pool 冷启动（池内容器创建
    # 时无此 env，Docker 无法向运行中容器补注入）。值可能是凭据，日志只记 key。
    env: Optional[dict[str, str]] = None
    # 可选：沙盒策略。缺省 = 联网（与历史行为一致）。每次 acquire 都会落实（复用沙盒
    # 不在对应网络上即热切网），调用方随每次工具调用带最新值即可。
    policy: Optional[PolicySpec] = None


class AcquireResponse(BaseModel):
    sandbox_id: str
    status: str
    effective_policy: Optional[EffectivePolicy] = None


class PolicyApplyRequest(BaseModel):
    """批量热应用策略：调用方业务键变更时调用，让在册运行中沙盒立即切网。"""
    policy: PolicySpec
    user_id: Optional[str] = None
    role_id: Optional[str] = None


class PolicyApplyResponse(BaseModel):
    effective_policy: EffectivePolicy
    switched: list[dict]
    failed: list[dict]


class StatusResponse(BaseModel):
    sandbox_id: str
    user_id: str
    role_id: str
    sandbox_type: str
    status: str
    container_ip: str
    acquired_at: str


class PingResponse(BaseModel):
    ok: bool
    status: str
    container_ip: str
    reachable: Optional[bool] = None


def _unavailable_detail(message: str, reason: str) -> dict:
    """503 的结构化 detail：附池容量/在用数快照，便于调用方区分限流与宕机（issue #4）。"""
    return {
        "message": message,
        "reason": reason,
        "warm_pool": _warm_pool.status() if _warm_pool else {},
        "sandboxes_ready": len(_registry.list_ready()) if _registry else 0,
    }


# ── 网络策略解析 / 落实 ───────────────────────────────────────────────────────

def _resolve_policy(policy: Optional[PolicySpec]) -> tuple[NetworkMode, EffectivePolicy]:
    """把请求策略解析成 (网络模式, 生效策略)。不支持的项拒绝（400）而不是降级。"""
    if policy is None:
        return "allow", EffectivePolicy(network=NetworkPolicySpec())
    if policy.network.allow or policy.network.deny:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "network allow/deny lists are not supported (capabilities.network_policy=binary)",
                "reason": "network_policy_lists_unsupported",
            },
        )
    mode: NetworkMode = policy.network.default
    if mode == "deny" and not settings.network_policy_supported:
        raise HTTPException(
            status_code=400,
            detail={
                "message": (
                    f"network policy unavailable: SANDBOX_NETWORK={settings.SANDBOX_NETWORK!r} is a "
                    "Docker built-in network; configure a user-defined network (default cr-sb-net)"
                ),
                "reason": "network_policy_unsupported",
            },
        )
    reasons: list[str] = []
    if policy.protected_paths:
        reasons.append("protected_paths not enforced in this version")
    if policy.writable_roots:
        reasons.append("writable_roots not enforced in this version")
    return mode, EffectivePolicy(
        network=NetworkPolicySpec(default=mode),
        protected_paths=policy.protected_paths,
        writable_roots=policy.writable_roots,
        reasons=reasons,
    )


async def _ensure_network(info: ContainerInfo, mode: NetworkMode) -> bool:
    """把容器落到策略对应网络；成功返回 True，失败记 warning 返回 False（调用方销毁重建）。"""
    old_ip = info.container_ip
    try:
        await _container_manager.switch_network(info, mode)
    except Exception as e:
        logger.warning(f"切网失败 | name={info.container_name} | mode={mode} | err={e}")
        return False
    if info.container_ip != old_ip:
        try:
            await close_client(old_ip)
        except Exception:
            pass
    return True


# ── 接口 ──────────────────────────────────────────────────────────────────────

@router.post("/acquire", response_model=AcquireResponse)
async def acquire_sandbox(req: AcquireRequest) -> AcquireResponse:
    """
    获取沙盒。

    1. 复用已有 ready sandbox（同 user+role）
    2. 从 warm pool 取一个
    3. 兜底：冷启动新容器
    后台异步补充 pool，不阻塞返回。
    每条路径都把容器落到 policy 对应的网络（复用/出池的热切网，冷启动直接落网）。
    """
    mode, effective = _resolve_policy(req.policy)
    target_network = settings.network_for_mode(mode)

    # 1. 复用（已分配的容器，无论挂载与否，直接复用——挂载随容器持续存在）。
    #    复用前体检：容器可能已被 docker restart/OOM/手动删除干掉，registry 只是内存
    #    记录感知不到；死沙盒就地驱逐销毁，落到下面的全新分配（对调用方透明自愈）。
    #    体检通过后落实网络策略：切网失败同样驱逐重建。
    existing = await _registry.find_active(req.user_id, req.role_id)
    if existing:
        if await _container_manager.is_healthy(existing.container_info.container_ip):
            if await _ensure_network(existing.container_info, mode):
                _registry.touch(existing.sandbox_id)
                logger.debug(f"复用 sandbox | id={existing.sandbox_id} | user={req.user_id}")
                return AcquireResponse(
                    sandbox_id=existing.sandbox_id, status="ready", effective_policy=effective
                )
            logger.warning(f"复用沙盒切网失败，驱逐重建 | id={existing.sandbox_id}")
        else:
            logger.warning(
                f"复用沙盒失联，驱逐重建 | id={existing.sandbox_id} "
                f"| ip={existing.container_info.container_ip} | user={req.user_id}"
            )
        await _reconciler.destroy_sandbox(existing)

    # 2. 决定是否挂载工作区。请求带 workspace 但本服务无挂载能力（开关关/缺 MinIO 凭据）
    #    时记 warning 并回退为不挂载，避免因配置缺失而让终端整体不可用。
    workspace = None
    if req.workspace is not None:
        if settings.mount_ready:
            workspace = WorkspaceMount(
                bucket=req.workspace.bucket,
                prefix=req.workspace.prefix,
                mount_path=req.workspace.mount_path,
            )
        else:
            logger.warning(
                "请求工作区挂载但本服务未就绪（WORKSPACE_MOUNT_ENABLED/MinIO 凭据），"
                f"回退为不挂载 | user={req.user_id} | role={req.role_id}"
            )

    # 3. 环境变量注入（issue #15/#16）：env 只能在容器创建时注入，故非空即冷启动。
    #    值可能是凭据（scoped token），日志只记 key 数量/名单，绝不打印 value。
    extra_env = req.env or None
    if extra_env:
        logger.info(
            f"acquire 携带 env 注入 | keys={sorted(extra_env)} "
            f"| user={req.user_id} | role={req.role_id}"
        )

    # 4. 挂载容器：专属、不走 warm pool（mount 须在创建时带 FUSE 能力），冷启动。
    if workspace is not None:
        try:
            container = await _container_manager.run_container(
                req.sandbox_type, workspace=workspace, extra_env=extra_env, network=target_network
            )
        except Exception as e:
            logger.error(f"挂载容器冷启动失败 | type={req.sandbox_type} | err={e}")
            raise HTTPException(
                status_code=503,
                detail=_unavailable_detail(
                    f"mounted cold start failed: {e}", "mounted_cold_start_failed"
                ),
            )
        record = await _registry.register(container, req.user_id, req.role_id)
        logger.info(
            f"挂载 sandbox 已分配 | id={record.sandbox_id} | ip={container.container_ip} "
            f"| mount={workspace.bucket}/{workspace.prefix}->{workspace.mount_path}"
            f" | network={target_network}"
        )
        return AcquireResponse(
            sandbox_id=record.sandbox_id, status="ready", effective_policy=effective
        )

    # 5. 非挂载：无 env 时 warm pool 优先（出池体检 + 切网，失败销毁换下一个）；
    #    带 env 时绕过 warm pool（池内容器创建时无该 env），直接冷启动落目标网络。
    container = None if extra_env else await _pop_healthy(req.sandbox_type, mode)
    if container is None:
        if not extra_env:
            logger.warning(f"warm pool 为空，冷启动 | type={req.sandbox_type}")
        try:
            container = await _container_manager.run_container(
                req.sandbox_type, extra_env=extra_env, network=target_network
            )
        except Exception as e:
            logger.error(f"冷启动失败 | type={req.sandbox_type} | err={e}")
            raise HTTPException(
                status_code=503,
                detail=_unavailable_detail(f"cold start failed: {e}", "cold_start_failed"),
            )

    record = await _registry.register(container, req.user_id, req.role_id)

    # 后台补充 pool
    asyncio.create_task(_warm_pool.ensure_pool(req.sandbox_type))

    logger.info(
        f"sandbox 已分配 | id={record.sandbox_id} | ip={container.container_ip} "
        f"| network={container.network or target_network}"
    )
    return AcquireResponse(sandbox_id=record.sandbox_id, status="ready", effective_policy=effective)


async def _pop_healthy(sandbox_type: str, mode: NetworkMode = "allow") -> Optional[ContainerInfo]:
    """从 warm pool 弹容器、体检并落到策略网络：不健康或切网失败就销毁再弹下一个，
    绝不把死容器分配出去。"""
    while (container := await _warm_pool.acquire(sandbox_type)) is not None:
        if await _container_manager.is_healthy(container.container_ip):
            if await _ensure_network(container, mode):
                return container
            logger.warning(f"warm 容器切网失败，销毁换下一个 | name={container.container_name}")
        else:
            logger.warning(
                f"warm 容器不健康，销毁换下一个 | name={container.container_name} "
                f"| ip={container.container_ip}"
            )
        await _container_manager.remove_container(container.container_id)
    return None


@router.post("/policy/apply", response_model=PolicyApplyResponse)
async def apply_policy(req: PolicyApplyRequest) -> PolicyApplyResponse:
    """批量热应用策略：对在册 ready 沙盒（可按 user_id / role_id 过滤）逐个落实网络，
    立即生效、不等下一次 acquire。切网失败的沙盒驱逐销毁（下一次 acquire 重建）。"""
    mode, effective = _resolve_policy(req.policy)
    switched: list[dict] = []
    failed: list[dict] = []
    for record in _registry.list_ready():
        if req.user_id is not None and record.user_id != req.user_id:
            continue
        if req.role_id is not None and record.role_id != req.role_id:
            continue
        info = record.container_info
        before = info.network
        if await _ensure_network(info, mode):
            if info.network != before:
                switched.append({"sandbox_id": record.sandbox_id, "container_ip": info.container_ip})
        else:
            failed.append({"sandbox_id": record.sandbox_id, "error": "network switch failed"})
            await _reconciler.destroy_sandbox(record)
    logger.info(
        f"策略批量应用 | mode={mode} | switched={len(switched)} | failed={len(failed)}"
        + (f" | user={req.user_id}" if req.user_id else "")
    )
    return PolicyApplyResponse(effective_policy=effective, switched=switched, failed=failed)


@router.post("/{sandbox_id}/release")
async def release_sandbox(sandbox_id: str) -> dict:
    """
    释放沙盒。后台清理容器 workspace，立即返回 ok。
    """
    container_info = await _registry.mark_released(sandbox_id)
    if container_info is None:
        raise HTTPException(status_code=404, detail=f"sandbox {sandbox_id!r} not found")

    # 后台清理并归还 pool
    asyncio.create_task(_warm_pool.release(container_info))
    logger.info(f"sandbox 已释放 | id={sandbox_id}")
    return {"ok": True}


@router.get("/{sandbox_id}/status", response_model=StatusResponse)
async def get_status(sandbox_id: str) -> StatusResponse:
    record = await _registry.get(sandbox_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"sandbox {sandbox_id!r} not found")
    return StatusResponse(
        sandbox_id=record.sandbox_id,
        user_id=record.user_id,
        role_id=record.role_id,
        sandbox_type=record.container_info.sandbox_type,
        status=record.status,
        container_ip=record.container_info.container_ip,
        acquired_at=record.acquired_at.isoformat(),
    )


@router.get("/{sandbox_id}/ping", response_model=PingResponse, response_model_exclude_none=True)
async def ping_sandbox(sandbox_id: str, deep: bool = False) -> PingResponse:
    """
    检查沙盒健康状态。

    deep=false（默认）：浅检查，仅查询 registry 是否存在且 status=ready。
    deep=true：在浅检查基础上，TCP 探测容器 API 端口是否可达。
    """
    record = await _registry.get(sandbox_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"sandbox {sandbox_id!r} not found")

    ip = record.container_info.container_ip
    if not deep:
        return PingResponse(ok=record.status == "ready", status=record.status, container_ip=ip)

    reachable = await _container_manager.is_healthy(ip)
    return PingResponse(ok=reachable, status=record.status, container_ip=ip, reachable=reachable)


@router.get("")
async def list_sandboxes() -> dict:
    records = _registry.list_all()
    return {
        "sandboxes": [
            {
                "sandbox_id": r.sandbox_id,
                "user_id": r.user_id,
                "role_id": r.role_id,
                "status": r.status,
                "sandbox_type": r.container_info.sandbox_type,
                "container_name": r.container_info.container_name,
                "acquired_at": r.acquired_at.isoformat(),
                "last_active_at": r.last_active_at.isoformat(),
            }
            for r in records
        ]
    }
