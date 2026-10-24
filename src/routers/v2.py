"""
v2 接口：按工作区分配执行实例（ADR 0025）

- ``GET  /v2/capabilities``                       隔离能力逐项报告（?refresh=true 立即重测）
- ``POST /v2/work-areas/{id}/acquire``            分配 / 复用实例（409 忙碌或超限，503 隔离前提不满足）
- ``POST /v2/sandboxes/{id}/release``             释放实例（销毁容器，工作区保留）
- ``PUT  /v2/sandboxes/{id}/inputs|context``      就地更新只读视图
- ``GET  /v2/sandboxes/{id}/activity``            终端活性（不刷新活跃时间）
- ``*    /v2/sandboxes/{id}/proxy/{path}``         透传到实例 API（注入每实例 token；终端任务注入来源）
- ``GET  /v2/work-areas/{id}/usage|files|files/content``  经 cr-fs 读取，不依赖实例存活
- ``GET  /v2/work-areas/{id}/jobs``               终端任务来源清单（session_id / turn_id / 起止时间）
- ``POST /v2/work-areas/{id}/import``             tar 导入（迁移用）
- ``DELETE /v2/work-areas/{id}``                  删除工作区
- ``POST|GET|DELETE /v2/admin/drain``             切换窗口：排空 / 查询 / 解除 v1

除 capabilities 外全部要求 Hub 配置了 SANDBOX_HUB_API_KEY（全局中间件校验 X-API-Key）。
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from src.config import settings
from src.manager.fs_sidecar import FsOpError
from src.manager.wa_instances import CWD, LAYOUT_VERSION, AcquireSpec, WorkAreaError, WorkAreaService
from src.manager.wa_jobs import TERMINAL_START_PATHS, iso_utc
from src.models import InputFileSpec, RoleMountSpec
from src.proxy.forwarder import forward
from src.routers import sandboxes as v1

router = APIRouter(prefix="/v2")

_service: Optional[WorkAreaService] = None
_v1_registry = None
_warm_pool = None
_reconciler = None


def set_dependencies(service, v1_registry=None, warm_pool=None, reconciler=None) -> None:
    global _service, _v1_registry, _warm_pool, _reconciler
    _service = service
    _v1_registry = v1_registry
    _warm_pool = warm_pool
    _reconciler = reconciler
    if settings.files_cutover_active:
        v1.set_draining(True)
        if warm_pool is not None:
            warm_pool.paused = True


def _svc() -> WorkAreaService:
    if _service is None:
        raise HTTPException(status_code=503, detail={"reason": "not_ready", "message": "v2 未初始化"})
    return _service


def _require_hub_auth() -> None:
    if not settings.SANDBOX_HUB_API_KEY:
        raise HTTPException(
            status_code=503,
            detail={"reason": "hub_api_auth_required", "message": "v2 要求配置 SANDBOX_HUB_API_KEY"},
        )


_auth = [Depends(_require_hub_auth)]


def _error(exc: WorkAreaError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail()})


def _fs_error(exc: FsOpError) -> JSONResponse:
    if exc.not_found:
        status, reason = 404, "not_found"
    elif exc.invalid:
        status, reason = 400, "invalid_path"
    elif exc.not_regular:
        status, reason = 409, "not_regular_file"
    else:
        status, reason = 500, "fs_error"
    return JSONResponse(status_code=status, content={"detail": {"reason": reason, "message": str(exc)}})


# ── 模型 ──────────────────────────────────────────────────────────────────────


class ChecksumsRequest(BaseModel):
    paths: list[Annotated[str, Field(min_length=1, max_length=1024)]] = Field(
        min_length=1, max_length=128
    )


class RoleMountModel(BaseModel):
    name: str
    bucket: str
    prefix: str
    mode: Literal["ro", "rw"] = "ro"


class InputFileModel(BaseModel):
    path: str
    bucket: str
    key: str
    sha256: str
    size: int = Field(ge=0)


class LimitsModel(BaseModel):
    memory_mb: int = Field(default=0, ge=0)
    cpus: float = Field(default=0, ge=0)
    pids: int = Field(default=0, ge=0)


class AcquireV2Request(BaseModel):
    user_id: str
    role_id: str
    sandbox_type: Literal["ubuntu", "code"] = "code"
    session_id: str = ""
    turn_id: str = ""
    layout_version: int = LAYOUT_VERSION
    cwd: str = CWD
    mounts: list[RoleMountModel] = []
    # None = 不改（复用时保留现状）；[] / {} = 清空
    inputs: Optional[list[InputFileModel]] = None
    context: Optional[dict[str, str]] = None
    env: Optional[dict[str, str]] = None
    policy: Optional[v1.PolicySpec] = None
    limits: LimitsModel = LimitsModel()


class InputsRequest(BaseModel):
    files: list[InputFileModel]


class ContextRequest(BaseModel):
    files: dict[str, str]


class DrainRequest(BaseModel):
    wait_seconds: int = Field(default=600, ge=0, le=3600)
    force: bool = False


def _input_specs(files: Optional[list[InputFileModel]]) -> Optional[list[InputFileSpec]]:
    if files is None:
        return None
    return [InputFileSpec(path=f.path, bucket=f.bucket, key=f.key, sha256=f.sha256.lower(), size=f.size) for f in files]


# ── 能力 ──────────────────────────────────────────────────────────────────────


@router.get("/capabilities")
async def capabilities(refresh: bool = False) -> dict:
    service = _svc()
    if refresh:
        return await service.run_selftest()
    return service.capabilities()


# ── 实例 ──────────────────────────────────────────────────────────────────────


@router.post("/work-areas/{work_area_id}/acquire", dependencies=_auth)
async def acquire(work_area_id: str, req: AcquireV2Request):
    service = _svc()
    if req.layout_version != LAYOUT_VERSION:
        raise HTTPException(400, detail={"reason": "layout_version_unsupported", "message": f"仅支持 {LAYOUT_VERSION}"})
    if req.cwd != CWD:
        raise HTTPException(400, detail={"reason": "cwd_unsupported", "message": f"cwd 只能是 {CWD}"})
    mode, effective_policy = v1._resolve_policy(req.policy)
    spec = AcquireSpec(
        work_area_id=work_area_id,
        user_id=req.user_id,
        role_id=req.role_id,
        sandbox_type=req.sandbox_type,
        session_id=req.session_id,
        turn_id=req.turn_id,
        mounts=[RoleMountSpec(name=m.name, bucket=m.bucket, prefix=m.prefix, mode=m.mode) for m in req.mounts],
        inputs=_input_specs(req.inputs),
        context=req.context,
        env=req.env,
        network_mode=mode,
        limits=req.limits.model_dump(),
    )
    try:
        instance, reused = await service.acquire(spec)
    except WorkAreaError as exc:
        return _error(exc)
    return {
        "sandbox_id": instance.sandbox_id,
        "work_area_id": instance.work_area_id,
        "status": "ready",
        "reused": reused,
        "effective": service.effective_report(instance),
        "effective_policy": effective_policy.model_dump(),
    }


@router.get("/sandboxes", dependencies=_auth)
async def list_instances() -> dict:
    service = _svc()
    return {"sandboxes": [dict(r.holder(), sandbox_type=r.sandbox_type) for r in service.registry.list_all()]}


@router.post("/sandboxes/policy/apply", dependencies=_auth)
async def apply_policy(req: v1.PolicyApplyRequest) -> dict:
    """业务侧网络开关热改：在册实例立即切网，不等下一次 acquire。"""
    mode, effective = v1._resolve_policy(req.policy)
    switched, failed = await _svc().apply_network_all(
        mode, user_id=req.user_id, role_id=req.role_id
    )
    logger.info(f"v2 策略批量应用 | mode={mode} | switched={len(switched)} | failed={len(failed)}")
    return {"effective_policy": effective.model_dump(), "switched": switched, "failed": failed}


@router.get("/sandboxes/{sandbox_id}", dependencies=_auth)
async def instance_status(sandbox_id: str):
    service = _svc()
    try:
        instance = service.get(sandbox_id)
    except WorkAreaError as exc:
        return _error(exc)
    return dict(instance.holder(), sandbox_type=instance.sandbox_type, effective=service.effective_report(instance))


@router.post("/sandboxes/{sandbox_id}/release", dependencies=_auth)
async def release(sandbox_id: str):
    try:
        instance = await _svc().release(sandbox_id)
    except WorkAreaError as exc:
        return _error(exc)
    return {"ok": True, "sandbox_id": sandbox_id, "work_area_id": instance.work_area_id}


@router.post("/sandboxes/{sandbox_id}/quiesce", dependencies=_auth)
async def quiesce(sandbox_id: str):
    """归档前停止实例进程，保留工作区与挂载；随后由平台归档并 release。"""
    try:
        await _svc().quiesce(sandbox_id)
    except WorkAreaError as exc:
        return _error(exc)
    return {"ok": True, "sandbox_id": sandbox_id}


@router.put("/sandboxes/{sandbox_id}/inputs", dependencies=_auth)
async def put_inputs(sandbox_id: str, req: InputsRequest):
    try:
        result = await _svc().update_inputs(sandbox_id, _input_specs(req.files) or [])
    except WorkAreaError as exc:
        return _error(exc)
    return {"ok": True, **result}


@router.put("/sandboxes/{sandbox_id}/context", dependencies=_auth)
async def put_context(sandbox_id: str, req: ContextRequest):
    try:
        files = await _svc().update_context(sandbox_id, req.files)
    except WorkAreaError as exc:
        return _error(exc)
    return {"ok": True, "files": files}


@router.get("/sandboxes/{sandbox_id}/activity", dependencies=_auth)
async def activity(sandbox_id: str):
    service = _svc()
    try:
        instance = service.get(sandbox_id)
    except WorkAreaError as exc:
        return _error(exc)
    return await service.activity(instance)


@router.api_route(
    "/sandboxes/{sandbox_id}/proxy/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    dependencies=_auth,
)
async def proxy(sandbox_id: str, path: str, request: Request) -> Response:
    service = _svc()
    try:
        instance = service.get(sandbox_id)
    except WorkAreaError as exc:
        return _error(exc)
    service.registry.touch(sandbox_id)
    info = instance.container_info
    if request.method == "POST" and path.strip("/") in TERMINAL_START_PATHS:
        # 终端任务（含随后转入后台的）按请求时实例当前的会话与回合打标
        async with service.terminal_start(instance):
            return await forward(
                info.container_ip,
                path,
                request,
                api_token=instance.api_token,
                extra_headers=service.origin_header(instance),
            )
    return await forward(info.container_ip, path, request, api_token=instance.api_token)


# ── 工作区文件（经 cr-fs，不依赖实例存活）────────────────────────────────────


@router.get("/work-areas/{work_area_id}/usage", dependencies=_auth)
async def usage(work_area_id: str, fresh: bool = False):
    try:
        return await _svc().work_areas.usage(work_area_id, fresh=fresh)
    except ValueError as exc:
        raise HTTPException(400, detail={"reason": "invalid_request", "message": str(exc)})
    except FsOpError as exc:
        return _fs_error(exc)


@router.get("/work-areas/{work_area_id}/files", dependencies=_auth)
async def list_files(
    work_area_id: str,
    root: Literal["work", "deliverables"] = "deliverables",
    max_files: Optional[int] = Query(default=None, ge=1),
):
    """``observed_at``：开始列举前取的 Hub 时钟，与 ``/jobs`` 的时间同源可比。"""
    observed_at = iso_utc(time.time())
    try:
        result = await _svc().work_areas.list_files(work_area_id, root, max_files)
    except ValueError as exc:
        raise HTTPException(400, detail={"reason": "invalid_request", "message": str(exc)})
    except FsOpError as exc:
        return _fs_error(exc)
    return {**result, "observed_at": observed_at}


@router.get("/work-areas/{work_area_id}/jobs", dependencies=_auth)
async def list_jobs(work_area_id: str):
    """工作区终端任务来源。``ended_at=null`` = 仍在运行或结束时间未知（按运行到 ``now``）；
    Hub 无法保证清单完整时 ``known=false``。"""
    try:
        return await _svc().job_report(work_area_id)
    except WorkAreaError as exc:
        return _error(exc)


@router.get("/work-areas/{work_area_id}/files/content", dependencies=_auth)
async def file_content(
    work_area_id: str,
    path: str,
    root: Literal["work", "deliverables"] = "deliverables",
):
    """文件内容 + 头 ``X-File-Size`` / ``X-File-Mtime-Ns`` / ``X-File-Sha256`` / ``X-File-Stable``
    （读取前后 size、mtime 不变为 1）。归档点据此判定稳定性。"""
    try:
        result = await _svc().work_areas.read(work_area_id, root, path)
    except ValueError as exc:
        raise HTTPException(400, detail={"reason": "invalid_path", "message": str(exc)})
    except FsOpError as exc:
        return _fs_error(exc)
    return FileResponse(
        result.path,
        media_type="application/octet-stream",
        headers={
            "X-File-Size": str(result.size),
            "X-File-Mtime-Ns": str(result.mtime_ns),
            "X-File-Sha256": result.sha256,
            "X-File-Stable": "1" if result.stable else "0",
        },
        background=BackgroundTask(os.unlink, result.path),
    )


@router.post("/work-areas/{work_area_id}/import", dependencies=_auth)
async def import_files(
    work_area_id: str, request: Request, root: Literal["work", "deliverables"] = "work"
):
    """请求体为 tar 流（application/x-tar）；只接受普通文件与目录。"""
    service = _svc()
    if service.is_deleting(work_area_id):
        return _error(WorkAreaError(409, "work_area_deleting", "工作区正在删除"))
    try:
        result = await service.work_areas.import_tar(work_area_id, root, request.stream())
    except ValueError as exc:
        raise HTTPException(400, detail={"reason": "invalid_request", "message": str(exc)})
    except FsOpError as exc:
        return _fs_error(exc)
    return {"ok": True, **result}


@router.post("/work-areas/{work_area_id}/files/checksums", dependencies=_auth)
async def file_checksums(
    work_area_id: str, payload: ChecksumsRequest,
    root: Literal["work", "deliverables"] = "work",
):
    """按显式路径批量核对指纹；保留普通文件、稳定性和路径隔离检查。"""
    try:
        return await _svc().work_areas.checksums(work_area_id, root, payload.paths)
    except ValueError as exc:
        raise HTTPException(400, detail={"reason": "invalid_path", "message": str(exc)})
    except FsOpError as exc:
        return _fs_error(exc)


@router.delete("/work-areas/{work_area_id}", dependencies=_auth)
async def delete_work_area(work_area_id: str, force: bool = False):
    try:
        result = await _svc().delete_work_area(work_area_id, force=force)
    except WorkAreaError as exc:
        return _error(exc)
    except FsOpError as exc:
        return _fs_error(exc)
    return {"ok": True, **result}


# ── 切换窗口：排空 v1 ────────────────────────────────────────────────────────


def _drain_status() -> dict:
    ready = _v1_registry.list_ready() if _v1_registry else []
    return {
        "draining": v1.is_draining(),
        "files_cutover": settings.files_cutover_active,
        "v1_ready": [
            {"sandbox_id": r.sandbox_id, "user_id": r.user_id, "role_id": r.role_id} for r in ready
        ],
        "warm_pool": _warm_pool.status() if _warm_pool else {},
    }


@router.get("/admin/drain", dependencies=_auth)
async def drain_status() -> dict:
    return _drain_status()


@router.post("/admin/drain", dependencies=_auth)
async def drain(req: DrainRequest) -> dict:
    """停止接受 v1 acquire、停补并清空 warm pool；等待 v1 实例的后台 job 结束后逐个销毁。
    超时仍忙的实例保留并列出（force=true 则一并销毁）。"""
    if _v1_registry is None or _reconciler is None or _warm_pool is None:
        raise HTTPException(503, detail={"reason": "not_ready", "message": "v1 依赖未注入"})
    v1.set_draining(True)
    _warm_pool.paused = True
    await _warm_pool.drain()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + req.wait_seconds
    released: list[str] = []
    busy: list[dict] = []
    while True:
        busy = []
        for record in _v1_registry.list_ready():
            idle = await _reconciler._probe_terminal_idle(record.container_info.container_ip)
            if idle is True:
                await _reconciler.destroy_sandbox(record)
                released.append(record.sandbox_id)
            else:
                busy.append({"sandbox_id": record.sandbox_id, "user_id": record.user_id, "activity_known": idle is not None})
        if not busy or loop.time() >= deadline:
            break
        await asyncio.sleep(min(5.0, max(0.0, deadline - loop.time())))
    if busy and req.force:
        for record in list(_v1_registry.list_ready()):
            await _reconciler.destroy_sandbox(record)
            released.append(record.sandbox_id)
        busy = []
    logger.info(f"v1 排空 | released={len(released)} | still_busy={len(busy)} | force={req.force}")
    return {**_drain_status(), "released": released, "still_busy": busy, "timed_out": bool(busy)}


@router.delete("/admin/drain", dependencies=_auth)
async def undrain() -> dict:
    """解除排空（回滚到 v1 时用）：恢复 v1 acquire 与 warm pool 补充。"""
    service = _svc()
    if settings.files_cutover_active:
        for instance in list(service.registry.list_all()):
            await service.release(instance.sandbox_id, reason="files_rollback")
        await service.set_files_cutover(False)
    v1.set_draining(False)
    if _warm_pool is not None:
        _warm_pool.paused = False
        for sandbox_type in settings.sandbox_types:
            asyncio.create_task(_warm_pool.ensure_pool(sandbox_type))
    return _drain_status()


@router.post("/admin/files-cutover", dependencies=_auth)
async def files_cutover() -> dict:
    """排空完成后关闭 MinIO 转发；持久化 v2 专用状态，回滚经 DELETE /admin/drain。"""
    status = _drain_status()
    if not status['draining'] or status['v1_ready'] or _svc().registry.list_all():
        raise HTTPException(409, detail={'reason': 'not_drained', 'message': 'Hub 尚未排空，拒绝切换'})
    if _warm_pool is None or not _warm_pool.paused:
        raise HTTPException(409, detail={'reason': 'warm_pool_running', 'message': '预热池尚未暂停'})
    await _svc().set_files_cutover(True)
    return {**_drain_status(), 'minio_forward': False}
