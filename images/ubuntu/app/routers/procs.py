"""
长驻进程路由（SandboxHub#52）——执行引擎这类经 stdio 驱动、跨多个回合的进程。

接口列表：
- POST   /api/procs                   启动（201 → ProcessInfo；409 proc_exists；400 invalid_request）
- GET    /api/procs?name=<name>       列出（缺省全部）
- GET    /api/procs/{proc_id}         状态（404 proc_not_found）
- POST   /api/procs/{proc_id}/stdin   写 stdin，全部写进管道才返回（背压）；可同时关闭
- POST   /api/procs/{proc_id}/read    按字节游标长轮询 stdout / stderr：有新输出或进程已结束立即返回，
                                      否则最多等 ``wait`` 秒（上限 120）
- POST   /api/procs/{proc_id}/signal  向进程组发 INT / TERM / HUP / KILL
- DELETE /api/procs/{proc_id}         回收：仍在运行先 KILL 进程组，再删记录与日志

错误体统一为 ``{"detail": {"reason": ..., "message": ...}}``，请求体形状不对同样按
400 ``invalid_request`` 返回。语义（环境隔离、日志保留、结束判定）见 ``app/tools/procs.py``。
"""

import base64
import binascii

from fastapi import APIRouter, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from ..tools.procs import FileSpec, ProcError, ProcManager, StartSpec

# 全局进程管理器（应用启动时注入；测试可直接替换）
proc_manager: ProcManager | None = None


def get_manager() -> ProcManager:
    global proc_manager
    if proc_manager is None:
        proc_manager = ProcManager()
    return proc_manager


def _error(status: int, reason: str, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": {"reason": reason, "message": message, **extra}})


class _ProcRoute(APIRoute):
    """请求体校验失败与 ProcError 统一转成 ``{"detail": {"reason", "message"}}``。"""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def route(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                message = "; ".join(
                    f"{'.'.join(str(part) for part in err.get('loc', ()))}: {err.get('msg', '')}"
                    for err in exc.errors()
                )
                return _error(400, "invalid_request", message or "请求体无效")
            except ProcError as exc:
                return _error(exc.status, exc.reason, exc.message, **exc.extra)

        return route


router = APIRouter(prefix="/api/procs", tags=["长驻进程"], route_class=_ProcRoute)


# ==================== 请求/响应模型 ====================


class ProcFile(BaseModel):
    path: str = Field(..., description="容器内绝对路径；父目录按需创建（0700），原子替换")
    content_b64: str = Field(default="", description="文件内容（base64）")
    mode: int = Field(default=0o600, ge=0, le=0o7777, description="文件权限位（整数，如 384 = 0o600）")


class StartRequest(BaseModel):
    name: str = Field(..., description="调用方命名；同名的运行中进程只能有一个，同名的已结束进程在启动前回收")
    argv: list[str] = Field(..., min_length=1, description="argv[0] 为可执行文件（不含 / 时按 env 的 PATH 查找）")
    env: dict[str, str] = Field(default_factory=dict, description="进程环境（不继承容器默认环境）")
    inherit_env: list[str] = Field(
        default_factory=list, description="从容器默认环境继承的变量名白名单；与 env 同名时 env 优先"
    )
    cwd: str | None = Field(default=None, description="工作目录（绝对路径，须已存在）；缺省为终端初始目录")
    files: list[ProcFile] = Field(default_factory=list, description="启动前写入的文件")
    reset_dirs: list[str] = Field(
        default_factory=list,
        description="写 files 之前整个删除的目录（规范化绝对路径；不能是顶层目录、挂载点或包含挂载点）",
    )
    log_retain_bytes: int = Field(default=0, ge=0, description="每路输出保证可读的最近字节数；0 = 不限")


class ProcInfo(BaseModel):
    proc_id: str = Field(description="p_<ULID>")
    name: str
    pid: int
    state: str = Field(description="running | exited | killed（我方经 signal / DELETE 发过信号后结束）")
    exit_code: int | None = Field(description="退出码；负数 = 被该号信号终止；运行中为 null")
    started_at: str = Field(description="ISO-8601 UTC（Z）")
    ended_at: str | None
    stdin_open: bool
    stdout_size: int = Field(description="自启动以来 stdout 总字节数（含已按保留量丢弃的部分）")
    stderr_size: int


class ProcList(BaseModel):
    procs: list[ProcInfo]


class StdinRequest(BaseModel):
    data_b64: str = Field(default="", description="要写入的字节（base64）")
    close: bool = Field(default=False, description="写完后关闭 stdin（引擎据此有序退出）")


class StdinResponse(BaseModel):
    written: int
    stdin_open: bool


class ReadRequest(BaseModel):
    stdout_cursor: int = Field(default=0, ge=0, description="stdout 绝对字节偏移")
    stderr_cursor: int = Field(default=0, ge=0, description="stderr 绝对字节偏移")
    wait: float = Field(default=0.0, ge=0.0, description="最多等待秒数（服务端上限 120）")
    max_bytes: int = Field(default=0, ge=0, description="每路单次最多返回字节数；0 = 默认 1 MiB（上限 16 MiB）")


class ReadResponse(BaseModel):
    stdout_b64: str
    stdout_cursor: int
    stderr_b64: str
    stderr_cursor: int
    truncated: bool = Field(description="游标早于保留窗口：数据从窗口起点开始，中间有缺口")
    proc: ProcInfo


class SignalRequest(BaseModel):
    signal: str = Field(..., description="INT | TERM | HUP | KILL")


class OkResponse(BaseModel):
    ok: bool


def _decode(value: str, what: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProcError(400, "invalid_request", f"{what} 不是合法的 base64：{exc}") from exc


# ==================== API 接口 ====================


@router.post("", status_code=201, response_model=ProcInfo, summary="启动长驻进程")
async def start_proc(request: StartRequest):
    spec = StartSpec(
        name=request.name,
        argv=tuple(request.argv),
        env=dict(request.env),
        inherit_env=tuple(request.inherit_env),
        cwd=request.cwd,
        files=tuple(
            FileSpec(path=f.path, content=_decode(f.content_b64, f"files[{i}].content_b64"), mode=f.mode)
            for i, f in enumerate(request.files)
        ),
        log_retain_bytes=request.log_retain_bytes,
        reset_dirs=tuple(request.reset_dirs),
    )
    return await get_manager().start(spec)


@router.get("", response_model=ProcList, summary="列出长驻进程")
async def list_procs(name: str | None = None):
    return {"procs": get_manager().list(name)}


@router.get("/{proc_id}", response_model=ProcInfo, summary="查询长驻进程")
async def get_proc(proc_id: str):
    return get_manager().get(proc_id).info()


@router.post("/{proc_id}/stdin", response_model=StdinResponse, summary="写入 / 关闭 stdin")
async def write_stdin(proc_id: str, request: StdinRequest):
    data = _decode(request.data_b64, "data_b64")
    return await get_manager().write_stdin(proc_id, data, request.close)


@router.post("/{proc_id}/read", response_model=ReadResponse, summary="按游标读取输出（长轮询）")
async def read_output(proc_id: str, request: ReadRequest):
    return await get_manager().read(
        proc_id, request.stdout_cursor, request.stderr_cursor, request.wait, request.max_bytes
    )


@router.post("/{proc_id}/signal", response_model=OkResponse, summary="向进程组发信号")
async def signal_proc(proc_id: str, request: SignalRequest):
    get_manager().signal(proc_id, request.signal)
    return {"ok": True}


@router.delete("/{proc_id}", response_model=OkResponse, summary="回收长驻进程")
async def delete_proc(proc_id: str):
    await get_manager().remove(proc_id)
    return {"ok": True}
