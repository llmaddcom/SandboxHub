"""
屏幕操作路由模块 - 提供屏幕截图和信息查询的 API 接口。

接口列表：
- GET /api/screen/screenshot: 截取屏幕截图
- GET /api/screen/cursor_position: 获取鼠标光标位置
- GET /api/screen/info: 获取屏幕分辨率等信息
- POST /api/screen/zoom: 缩放查看指定屏幕区域
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from computer_use_demo.tools import ToolError

# 创建屏幕操作路由，设置前缀和标签
router = APIRouter(prefix="/api/screen", tags=["屏幕操作"])

# 电脑工具实例（由 main.py 注入）
computer_tool = None


def get_computer_tool():
    """获取电脑工具实例。

    返回:
        ComputerTool: 电脑操作工具实例

    抛出:
        HTTPException: 如果工具未初始化
    """
    if computer_tool is None:
        raise HTTPException(status_code=500, detail="电脑操作工具未初始化")
    return computer_tool


# ==================== 请求/响应模型 ====================

class ScreenshotResponse(BaseModel):
    """截图响应模型。"""
    success: bool = Field(description="是否截图成功")
    base64_image: str | None = Field(default=None, description="Base64 编码的 PNG 截图")
    error: str | None = Field(default=None, description="错误信息")


class CursorPositionResponse(BaseModel):
    """光标位置响应模型。"""
    success: bool = Field(description="是否获取成功")
    x: int = Field(description="光标 X 坐标")
    y: int = Field(description="光标 Y 坐标")


class ScreenInfoResponse(BaseModel):
    """屏幕信息响应模型。"""
    display_width_px: int = Field(description="显示宽度（像素，API 坐标）")
    display_height_px: int = Field(description="显示高度（像素，API 坐标）")
    display_number: int | None = Field(default=None, description="X11 显示编号")
    actual_width_px: int = Field(description="实际屏幕宽度（像素）")
    actual_height_px: int = Field(description="实际屏幕高度（像素）")


class ZoomRequest(BaseModel):
    """缩放请求模型。"""
    x0: int = Field(..., ge=0, description="区域左上角 X 坐标")
    y0: int = Field(..., ge=0, description="区域左上角 Y 坐标")
    x1: int = Field(..., ge=0, description="区域右下角 X 坐标")
    y1: int = Field(..., ge=0, description="区域右下角 Y 坐标")


class ZoomResponse(BaseModel):
    """缩放响应模型。"""
    success: bool = Field(description="是否缩放成功")
    base64_image: str | None = Field(default=None, description="Base64 编码的裁剪截图")
    error: str | None = Field(default=None, description="错误信息")


# ==================== API 接口 ====================

@router.get("/screenshot", response_model=ScreenshotResponse, summary="截取屏幕截图")
async def take_screenshot():
    """截取当前屏幕的完整截图。

    返回 Base64 编码的 PNG 格式截图。
    如果启用了坐标缩放，截图会被调整到合适的分辨率。

    返回:
        ScreenshotResponse: 包含 base64 编码截图
    """
    try:
        tool = get_computer_tool()
        result = await tool.screenshot()
        return ScreenshotResponse(
            success=True,
            base64_image=result.base64_image,
            error=result.error,
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"截图失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"截图失败: {str(e)}")


@router.get("/cursor_position", response_model=CursorPositionResponse, summary="获取光标位置")
async def get_cursor_position():
    """获取当前鼠标光标在屏幕上的位置。

    返回的坐标为 API 坐标（经过缩放转换）。

    返回:
        CursorPositionResponse: 包含光标的 X、Y 坐标
    """
    try:
        tool = get_computer_tool()
        result = await tool.get_cursor_position()
        output = result.output or ""
        # 解析 "X=数值,Y=数值" 格式的输出
        x = int(output.split("X=")[1].split(",")[0])
        y = int(output.split("Y=")[1])
        return CursorPositionResponse(success=True, x=x, y=y)
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"获取光标位置失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取光标位置失败: {str(e)}")


@router.get("/info", response_model=ScreenInfoResponse, summary="获取屏幕信息")
async def get_screen_info():
    """获取屏幕分辨率和显示编号等信息。

    返回:
        ScreenInfoResponse: 包含屏幕尺寸和显示编号
    """
    try:
        tool = get_computer_tool()
        info = await tool.get_screen_info()
        return ScreenInfoResponse(**info)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取屏幕信息失败: {str(e)}")


@router.post("/zoom", response_model=ZoomResponse, summary="缩放查看屏幕区域")
async def zoom_screen(request: ZoomRequest):
    """缩放查看屏幕的指定矩形区域。

    对屏幕进行截图后裁剪到指定区域，用于放大查看细节。

    参数:
        request: 包含区域坐标 (x0, y0, x1, y1) 的请求体

    返回:
        ZoomResponse: 包含裁剪后的 base64 截图
    """
    try:
        tool = get_computer_tool()
        result = await tool.zoom((request.x0, request.y0, request.x1, request.y1))
        return ZoomResponse(
            success=True,
            base64_image=result.base64_image,
            error=result.error,
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"缩放失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"缩放失败: {str(e)}")
