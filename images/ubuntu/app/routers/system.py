"""
系统操作路由模块 - 提供健康检查、剪贴板和系统信息的 API 接口。

接口列表：
- GET  /api/system/health: 健康检查
- GET  /api/system/env: 运行时环境/工具链信息（语言版本、CLI、工作区）
- POST /api/system/wait: 等待指定时间后截图
- GET  /api/system/clipboard: 获取剪贴板内容
- POST /api/system/clipboard: 设置剪贴板内容
- GET  /api/system/info: 获取系统信息
"""

import asyncio
import os
import platform
import sys

import psutil
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import APP_VERSION
from ..tools import ToolError
from ..tools.run import run

router = APIRouter(prefix="/api/system", tags=["系统操作"])

computer_tool = None


def get_computer_tool():
    if computer_tool is None:
        raise HTTPException(status_code=500, detail="电脑操作工具未初始化")
    return computer_tool


# ==================== 请求/响应模型 ====================

class HealthResponse(BaseModel):
    status: str = Field(description="服务状态")
    message: str = Field(description="状态描述信息")
    app_version: str = Field(description="容器内 app 版本（来自镜像内 VERSION 文件），供部署漂移对账")


class WaitRequest(BaseModel):
    duration: float = Field(
        ...,
        gt=0,
        le=100,
        description="等待时间（秒），最大 100 秒",
        examples=[1.0, 3.0, 5.0],
    )


class WaitResponse(BaseModel):
    success: bool = Field(description="是否操作成功")
    base64_image: str | None = Field(default=None, description="等待后的截图（Base64 编码）")
    error: str | None = Field(default=None, description="错误信息")


class ClipboardGetResponse(BaseModel):
    success: bool = Field(description="是否获取成功")
    content: str = Field(description="剪贴板文本内容")


class ClipboardSetRequest(BaseModel):
    content: str = Field(..., description="要设置到剪贴板的文本内容")


class ClipboardSetResponse(BaseModel):
    success: bool = Field(description="是否设置成功")
    message: str = Field(description="操作结果信息")


class EnvInfoResponse(BaseModel):
    success: bool = Field(description="是否获取成功")
    profile: str = Field(description="当前沙盒 profile（code / desktop）")
    platform: str = Field(description="操作系统平台信息")
    python_version: str = Field(description="Python 版本")
    workspace: str = Field(description="工作区路径")
    workspace_writable: bool = Field(description="工作区是否可写")
    tools: dict[str, str | None] = Field(
        description="已安装命令行工具的版本（缺失则为 null），如 node/npm/pnpm/git/rg",
    )


class SystemInfoResponse(BaseModel):
    success: bool = Field(description="是否获取成功")
    cpu_count: int = Field(description="CPU 核心数")
    cpu_percent: float = Field(description="CPU 使用率 (%)")
    memory_total_mb: float = Field(description="总内存 (MB)")
    memory_used_mb: float = Field(description="已用内存 (MB)")
    memory_percent: float = Field(description="内存使用率 (%)")
    disk_total_gb: float = Field(description="磁盘总量 (GB)")
    disk_used_gb: float = Field(description="已用磁盘 (GB)")
    disk_percent: float = Field(description="磁盘使用率 (%)")


# ==================== API 接口 ====================

@router.get("/health", response_model=HealthResponse, summary="健康检查")
async def health_check():
    """检查服务是否正常运行，并暴露 app 版本供镜像漂移对账。"""
    return HealthResponse(
        status="healthy",
        message="沙盒操作服务运行正常",
        app_version=APP_VERSION,
    )


# 探测命令行工具版本：cmd -> 取版本的命令（多取首行，避免多行噪声污染）
_TOOL_VERSION_CMDS: dict[str, str] = {
    "node": "node --version",
    "npm": "npm --version",
    "pnpm": "pnpm --version",
    "yarn": "yarn --version",
    "git": "git --version",
    "rg": "rg --version",
    "rclone": "rclone version",
    "pip": "pip --version",
}


async def _probe_version(cmd: str) -> str | None:
    """运行版本命令，成功取首行非空输出，失败/缺失返回 None。"""
    try:
        code, stdout, _ = await run(cmd, timeout=5.0)
    except Exception:
        return None
    if code != 0:
        return None
    for line in stdout.splitlines():
        line = line.strip()
        if line:
            return line
    return None


@router.get("/env", response_model=EnvInfoResponse, summary="运行时环境/工具链信息")
async def get_env_info():
    """返回沙盒运行时环境：profile、语言版本、已装 CLI 工具版本与工作区可写性。

    供编码 Agent 在执行任务前自检环境（参考 opencode/codex 的环境探测），
    避免对不存在的工具发起调用。所有版本探测均为 best-effort，缺失工具返回 null。
    """
    workspace = os.getenv("SANDBOX_WORKSPACE", "/workspace")
    versions = await asyncio.gather(
        *(_probe_version(cmd) for cmd in _TOOL_VERSION_CMDS.values())
    )
    tools = dict(zip(_TOOL_VERSION_CMDS.keys(), versions))
    return EnvInfoResponse(
        success=True,
        profile=os.getenv("SANDBOX_PROFILE", "desktop"),
        platform=platform.platform(),
        python_version=sys.version.split()[0],
        workspace=workspace,
        workspace_writable=os.path.isdir(workspace) and os.access(workspace, os.W_OK),
        tools=tools,
    )


@router.post("/wait", response_model=WaitResponse, summary="等待并截图")
async def wait_and_screenshot(request: WaitRequest):
    """等待指定时间后截取屏幕截图。"""
    try:
        tool = get_computer_tool()
        result = await tool.wait(request.duration)
        return WaitResponse(
            success=True,
            base64_image=result.base64_image,
            error=result.error,
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"等待操作失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"等待操作失败: {str(e)}")


@router.get("/clipboard", response_model=ClipboardGetResponse, summary="获取剪贴板")
async def get_clipboard():
    """获取系统剪贴板的文本内容（通过 xclip）。"""
    try:
        tool = get_computer_tool()
        display_prefix = f"DISPLAY=:{tool.display_num} " if tool.display_num is not None else ""
        _, stdout, stderr = await run(
            f"{display_prefix}xclip -selection clipboard -o", timeout=5.0
        )
        return ClipboardGetResponse(success=True, content=stdout)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取剪贴板失败: {str(e)}")


@router.post("/clipboard", response_model=ClipboardSetResponse, summary="设置剪贴板")
async def set_clipboard(request: ClipboardSetRequest):
    """设置系统剪贴板的文本内容（通过 xclip）。"""
    try:
        tool = get_computer_tool()
        display_prefix = f"DISPLAY=:{tool.display_num} " if tool.display_num is not None else ""
        import shlex
        escaped = shlex.quote(request.content)
        _, _, stderr = await run(
            f"echo -n {escaped} | {display_prefix}xclip -selection clipboard", timeout=5.0
        )
        if stderr and "error" in stderr.lower():
            return ClipboardSetResponse(success=False, message=f"设置失败: {stderr}")
        return ClipboardSetResponse(success=True, message="剪贴板内容已更新")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"设置剪贴板失败: {str(e)}")


@router.get("/info", response_model=SystemInfoResponse, summary="系统信息")
async def get_system_info():
    """获取系统资源使用信息（CPU、内存、磁盘）。"""
    try:
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        return SystemInfoResponse(
            success=True,
            cpu_count=psutil.cpu_count() or 1,
            cpu_percent=psutil.cpu_percent(interval=0.5),
            memory_total_mb=round(mem.total / 1024 / 1024, 2),
            memory_used_mb=round(mem.used / 1024 / 1024, 2),
            memory_percent=mem.percent,
            disk_total_gb=round(disk.total / 1024 / 1024 / 1024, 2),
            disk_used_gb=round(disk.used / 1024 / 1024 / 1024, 2),
            disk_percent=disk.percent,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取系统信息失败: {str(e)}")
