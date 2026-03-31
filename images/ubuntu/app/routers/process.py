"""
进程与窗口管理路由模块 - 提供进程管理和窗口操作的 API 接口。

接口列表：
- GET  /api/process/list: 列出运行中的进程
- POST /api/process/kill: 终止指定进程
- GET  /api/window/list: 列出所有窗口
- POST /api/window/focus: 聚焦指定窗口
- POST /api/window/close: 关闭指定窗口
"""

import os
import signal

import psutil
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from computer_use_demo.tools import ToolError
from computer_use_demo.tools.run import run

router = APIRouter(tags=["进程与窗口管理"])

computer_tool = None


def get_computer_tool():
    if computer_tool is None:
        raise HTTPException(status_code=500, detail="电脑操作工具未初始化")
    return computer_tool


# ==================== 请求/响应模型 ====================


class ProcessInfo(BaseModel):
    pid: int = Field(description="进程 ID")
    name: str = Field(description="进程名称")
    cpu_percent: float | None = Field(default=None, description="CPU 使用率 (%)")
    memory_mb: float | None = Field(default=None, description="内存使用 (MB)")
    status: str = Field(description="进程状态")
    cmdline: str | None = Field(default=None, description="命令行参数")


class ProcessListResponse(BaseModel):
    success: bool = Field(description="是否获取成功")
    processes: list[ProcessInfo] = Field(description="进程列表")
    total: int = Field(description="进程总数")


class KillRequest(BaseModel):
    pid: int = Field(..., description="要终止的进程 ID")
    force: bool = Field(default=False, description="是否强制终止 (SIGKILL)")


class KillResponse(BaseModel):
    success: bool = Field(description="是否终止成功")
    message: str = Field(description="操作结果信息")


class WindowInfo(BaseModel):
    window_id: str = Field(description="窗口 ID")
    title: str = Field(description="窗口标题")


class WindowListResponse(BaseModel):
    success: bool = Field(description="是否获取成功")
    windows: list[WindowInfo] = Field(description="窗口列表")


class WindowActionRequest(BaseModel):
    window_id: str = Field(..., description="目标窗口 ID")


class WindowActionResponse(BaseModel):
    success: bool = Field(description="是否操作成功")
    message: str = Field(description="操作结果信息")
    base64_image: str | None = Field(default=None, description="操作后的截图（Base64 编码）")


# ==================== 进程接口 ====================


@router.get("/api/process/list", response_model=ProcessListResponse, summary="列出进程")
async def list_processes():
    """列出当前运行中的所有用户进程。"""
    try:
        processes = []
        for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "status", "cmdline"]):
            try:
                info = proc.info
                mem = info.get("memory_info")
                cmdline_parts = info.get("cmdline") or []
                processes.append(ProcessInfo(
                    pid=info["pid"],
                    name=info["name"] or "",
                    cpu_percent=info.get("cpu_percent"),
                    memory_mb=round(mem.rss / 1024 / 1024, 2) if mem else None,
                    status=info.get("status", "unknown"),
                    cmdline=" ".join(cmdline_parts)[:200] if cmdline_parts else None,
                ))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return ProcessListResponse(success=True, processes=processes, total=len(processes))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取进程列表失败: {str(e)}")


@router.post("/api/process/kill", response_model=KillResponse, summary="终止进程")
async def kill_process(request: KillRequest):
    """终止指定 PID 的进程。"""
    try:
        sig = signal.SIGKILL if request.force else signal.SIGTERM
        os.kill(request.pid, sig)
        return KillResponse(success=True, message=f"已发送 {'SIGKILL' if request.force else 'SIGTERM'} 到 PID {request.pid}")
    except ProcessLookupError:
        raise HTTPException(status_code=404, detail=f"进程 {request.pid} 不存在")
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"无权限终止进程 {request.pid}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"终止进程失败: {str(e)}")


# ==================== 窗口接口 ====================


@router.get("/api/window/list", response_model=WindowListResponse, summary="列出窗口")
async def list_windows():
    """列出桌面上所有可见窗口。"""
    try:
        tool = get_computer_tool()
        display_prefix = f"DISPLAY=:{tool.display_num} " if tool.display_num is not None else ""

        _, stdout, _ = await run(
            f"{display_prefix}xdotool search --onlyvisible --name ''", timeout=5.0
        )
        window_ids = [wid.strip() for wid in stdout.strip().split("\n") if wid.strip()]

        windows = []
        for wid in window_ids:
            _, title_out, _ = await run(
                f"{display_prefix}xdotool getwindowname {wid}", timeout=3.0
            )
            title = title_out.strip()
            if title:
                windows.append(WindowInfo(window_id=wid, title=title))

        return WindowListResponse(success=True, windows=windows)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取窗口列表失败: {str(e)}")


@router.post("/api/window/focus", response_model=WindowActionResponse, summary="聚焦窗口")
async def focus_window(request: WindowActionRequest):
    """聚焦（激活）指定的窗口。"""
    try:
        tool = get_computer_tool()
        display_prefix = f"DISPLAY=:{tool.display_num} " if tool.display_num is not None else ""

        _, stdout, stderr = await run(
            f"{display_prefix}xdotool windowactivate {request.window_id}", timeout=5.0
        )
        screenshot = await tool.screenshot()
        return WindowActionResponse(
            success=True,
            message=f"已聚焦窗口 {request.window_id}",
            base64_image=screenshot.base64_image,
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"聚焦窗口失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"聚焦窗口失败: {str(e)}")


@router.post("/api/window/close", response_model=WindowActionResponse, summary="关闭窗口")
async def close_window(request: WindowActionRequest):
    """关闭指定的窗口。"""
    try:
        tool = get_computer_tool()
        display_prefix = f"DISPLAY=:{tool.display_num} " if tool.display_num is not None else ""

        await run(
            f"{display_prefix}xdotool windowclose {request.window_id}", timeout=5.0
        )
        screenshot = await tool.screenshot()
        return WindowActionResponse(
            success=True,
            message=f"已关闭窗口 {request.window_id}",
            base64_image=screenshot.base64_image,
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"关闭窗口失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"关闭窗口失败: {str(e)}")
