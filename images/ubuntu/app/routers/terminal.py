"""
终端操作路由模块 - 提供终端命令执行的 API 接口（job 契约，issue #30）。

接口列表：
- POST /api/terminal/execute:        提交命令到持久会话，最多等 wait 秒后返回 job 状态
- POST /api/terminal/wait:           从 cursor 起取 job 增量输出，最多等 wait 秒
- POST /api/terminal/kill:           向 job 进程组发 INT / KILL
- POST /api/terminal/execute/stream: SSE 流式执行（旧契约，保留）
- POST /api/terminal/restart:        重启终端会话（kill 当前 job，cwd / env 复位）

兼容：请求体不带 ``wait`` 字段 = 旧契约（阻塞至结束，timeout 默认 30s / 上限 300s，
响应含 success/output/error/system）。带 ``wait`` = job 契约（timeout 无默认、无上限）。
两种形态的响应字段是同一超集，调用方按需取用。
"""

import json
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..tools import BashTool, ToolError
from ..tools.bash import DEFAULT_WAIT, MAX_WAIT, Job, SessionBusy

# 创建终端操作路由，设置前缀和标签
router = APIRouter(prefix="/api/terminal", tags=["终端操作"])

# 全局终端工具实例（在应用启动时初始化）
bash_tool: BashTool | None = None


def get_bash_tool() -> BashTool:
    """获取终端工具实例。

    返回:
        BashTool: 终端工具实例
    """
    global bash_tool
    if bash_tool is None:
        bash_tool = BashTool()
    return bash_tool


# ==================== 请求/响应模型 ====================

class ExecuteRequest(BaseModel):
    """执行命令请求模型。"""
    command: str = Field(..., description="要执行的 bash 命令", examples=["ls -la", "echo hello"])
    wait: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            f"本次请求最多等待秒数（job 契约，默认 {DEFAULT_WAIT:g}，服务端上限 {MAX_WAIT:g}）；"
            "命令没结束先返回 status=running，随后用 /wait 取结果。"
            "不带本字段 = 旧契约：阻塞至结束，timeout 默认 30s / 上限 300s。"
        ),
    )
    timeout: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "命令总时限（秒）。job 契约下无默认、无上限，到期 SIGINT→SIGKILL 前台进程组，"
            "不传 = 跑到命令自己结束；旧契约下默认 30s、上限 300s。"
        ),
    )


class JobResponse(BaseModel):
    """job 状态响应（execute / wait / kill 共用）。"""
    job_id: str = Field(description="job 标识")
    status: str = Field(description="running | exited | killed")
    exit_code: int | None = Field(default=None, description="命令退出码；未结束为 null")
    output: str = Field(default="", description="本次返回的输出片段（head/tail 截断）")
    cursor: int = Field(description="下次 /wait 的起始字节偏移（日志文件当前大小）")
    log_path: str = Field(description="全量输出日志路径，可在容器内 tail / grep")
    kill_reason: str | None = Field(
        default=None, description="被终止原因：timeout | kill:INT | kill:KILL | restart"
    )


class ExecuteResponse(JobResponse):
    """执行命令响应模型：job 字段 + 旧契约字段（success/output/error/system）。"""
    success: bool = Field(default=True, description="请求是否受理（命令结果看 exit_code）")
    error: str | None = Field(default=None, description="旧契约字段；stderr 已合并进 output")
    system: str | None = Field(default=None, description="系统级别提示信息")


class WaitRequest(BaseModel):
    """等待 job 请求模型。"""
    job_id: str = Field(..., description="execute 返回的 job_id")
    cursor: int = Field(default=0, ge=0, description="从该字节偏移起返回增量输出")
    wait: float = Field(
        default=DEFAULT_WAIT, ge=0.0,
        description=f"最多等待秒数（服务端上限 {MAX_WAIT:g}）；job 已结束立即返回",
    )


class KillRequest(BaseModel):
    """终止 job 请求模型。"""
    job_id: str = Field(..., description="execute 返回的 job_id")
    signal: str = Field(default="INT", description="INT | TERM | KILL")


class RestartResponse(BaseModel):
    """重启终端响应模型。"""
    success: bool = Field(description="是否重启成功")
    message: str = Field(description="重启结果信息")


def _job_payload(job: Job, start: int = 0) -> dict:
    output, cursor = job.read(start)
    return {
        "job_id": job.id,
        "status": job.status,
        "exit_code": job.exit_code,
        "output": output,
        "cursor": cursor,
        "log_path": str(job.log_path),
        "kill_reason": job.kill_reason,
    }


# ==================== API 接口 ====================

@router.post("/execute", response_model=ExecuteResponse, summary="执行 bash 命令")
async def execute_command(request: ExecuteRequest):
    """把命令送入持久会话执行。

    - job 契约（带 ``wait``）：最多等 ``wait`` 秒；没结束返回 ``status=running`` 与目前
      输出，随后用 ``/wait`` 分段长轮询。``timeout`` 无默认无上限。
      会话正忙（上一条命令未结束）返回 409，body 带正在跑的 job_id。
    - 旧契约（不带 ``wait``）：阻塞至结束，``timeout`` 默认 30s / 上限 300s。
    - cwd / 导出环境跨调用保留（``cd`` / ``export`` / ``source venv``）。
    """
    tool = get_bash_tool()
    try:
        if request.wait is None:
            # 旧契约：阻塞至结束
            job, result = await tool.execute_job(request.command, timeout=request.timeout)
            payload = _job_payload(job)
            payload["output"] = result.output or ""
            return ExecuteResponse(**payload, success=True, error=result.error, system=result.system)

        job = await tool.submit(request.command, wait=request.wait, timeout=request.timeout)
        return ExecuteResponse(**_job_payload(job), success=True, system=tool.pop_notes())
    except SessionBusy as e:
        raise HTTPException(
            status_code=409,
            detail={
                "message": e.message,
                "job_id": e.job.id,
                "status": e.job.status,
                "log_path": str(e.job.log_path),
            },
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"终端错误: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"执行命令失败: {str(e)}")


@router.post("/wait", response_model=JobResponse, summary="等待 job 并取增量输出")
async def wait_job(request: WaitRequest):
    """从 ``cursor`` 起返回增量输出；job 已结束立即返回 exited/killed + exit_code，
    否则最多等 ``wait`` 秒。同一 job 可反复 wait 直到结束。"""
    tool = get_bash_tool()
    try:
        job = await tool.wait(request.job_id, wait=request.wait)
    except ToolError as e:
        raise HTTPException(status_code=404, detail=f"终端错误: {e.message}")
    return JobResponse(**_job_payload(job, request.cursor))


@router.post("/kill", response_model=JobResponse, summary="终止 job")
async def kill_job(request: KillRequest):
    """向 job 的前台进程组发信号（INT 温和、KILL 强制）。已结束的 job 无操作。
    调用方在用户「停止」时调用，保证没有 timeout 的命令不会在用户离开后继续跑。"""
    tool = get_bash_tool()
    try:
        job = await tool.kill(request.job_id, sig=request.signal)
    except ToolError as e:
        status = 404 if "未知 job" in e.message else 400
        raise HTTPException(status_code=status, detail=f"终端错误: {e.message}")
    return JobResponse(**_job_payload(job))


@router.post("/execute/stream", summary="流式执行 bash 命令 (SSE)")
async def execute_command_stream(request: ExecuteRequest):
    """流式执行 bash 命令，通过 SSE 实时推送输出（旧契约：timeout 默认 30s / 上限 300s）。

    事件格式：
      data: {"type": "stdout", "chunk": "..."}
      data: {"type": "stderr", "chunk": "..."}
      data: {"type": "done"}
    """
    async def event_gen():
        try:
            async for event in get_bash_tool().execute_stream(request.command, request.timeout):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            err_event = {"type": "error", "chunk": str(e)}
            yield f"data: {json.dumps(err_event, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.post("/restart", response_model=RestartResponse, summary="重启终端会话")
async def restart_terminal():
    """重启终端会话：kill 正在跑的 job（SIGKILL），cwd / 环境变量恢复初始值。

    返回:
        RestartResponse: 包含重启结果信息
    """
    try:
        tool = get_bash_tool()
        result = await tool.restart()
        return RestartResponse(
            success=True,
            message=result.system or "终端已重启",
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"终端错误: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重启终端失败: {str(e)}")
