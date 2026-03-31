"""
系统操作路由模块 - 提供健康检查、剪贴板和系统信息的 API 接口。

接口列表：
- GET  /api/system/health: 健康检查
- POST /api/system/wait: 等待指定时间后截图
- GET  /api/system/clipboard: 获取剪贴板内容
- POST /api/system/clipboard: 设置剪贴板内容
- GET  /api/system/info: 获取系统信息
"""

import psutil
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

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
    """检查服务是否正常运行。"""
    return HealthResponse(
        status="healthy",
        message="沙盒操作服务运行正常",
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
