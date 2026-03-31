"""
终端操作路由模块 - 提供终端命令执行的 API 接口。

接口列表：
- POST /api/terminal/execute: 执行 bash 命令
- POST /api/terminal/restart: 重启终端会话
"""

import json
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..tools import BashTool, ToolError

# 创建终端操作路由，设置前缀和标签
router = APIRouter(prefix="/api/terminal", tags=["终端操作"])

# 全局终端工具实例（在应用启动时初始化）
bash_tool: BashTool | None = None


def get_bash_tool() -> BashTool:
    """获取终端工具实例。

    返回:
        BashTool: 终端工具实例

    抛出:
        HTTPException: 如果终端工具未初始化
    """
    global bash_tool
    if bash_tool is None:
        bash_tool = BashTool()
    return bash_tool


# ==================== 请求/响应模型 ====================

class ExecuteRequest(BaseModel):
    """执行命令请求模型。"""
    command: str = Field(..., description="要执行的 bash 命令", examples=["ls -la", "echo hello"])
    timeout: float | None = Field(
        default=None,
        ge=1.0,
        le=300.0,
        description="超时秒数，默认 30s，最大 300s",
    )


class ExecuteResponse(BaseModel):
    """执行命令响应模型。"""
    success: bool = Field(description="是否执行成功")
    output: str | None = Field(default=None, description="命令标准输出")
    error: str | None = Field(default=None, description="命令错误输出")
    system: str | None = Field(default=None, description="系统级别提示信息")


class RestartResponse(BaseModel):
    """重启终端响应模型。"""
    success: bool = Field(description="是否重启成功")
    message: str = Field(description="重启结果信息")


# ==================== API 接口 ====================

@router.post("/execute", response_model=ExecuteResponse, summary="执行 bash 命令")
async def execute_command(request: ExecuteRequest):
    """在终端中执行一条 bash 命令。

    支持执行任意 bash 命令，返回标准输出和错误输出。
    如果终端会话尚未启动，会自动创建。
    支持 per-request 超时覆盖（1-300s），默认 30s。

    参数:
        request: 包含要执行的命令的请求体

    返回:
        ExecuteResponse: 包含命令执行结果
    """
    try:
        tool = get_bash_tool()
        result = await tool.execute(request.command, timeout=request.timeout)
        return ExecuteResponse(
            success=True,
            output=result.output,
            error=result.error,
            system=result.system,
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"终端错误: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"执行命令失败: {str(e)}")


@router.post("/execute/stream", summary="流式执行 bash 命令 (SSE)")
async def execute_command_stream(request: ExecuteRequest):
    """流式执行 bash 命令，通过 SSE 实时推送输出。

    事件格式：
      data: {"type": "stdout", "chunk": "..."}
      data: {"type": "stderr", "chunk": "..."}
      data: {"type": "done"}
    """
    async def event_gen():
        async for event in get_bash_tool().execute_stream(request.command, request.timeout):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.post("/restart", response_model=RestartResponse, summary="重启终端会话")
async def restart_terminal():
    """重启终端会话。

    停止当前终端进程，创建并启动新的 bash 会话。
    适用于终端会话超时或异常退出的情况。

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
