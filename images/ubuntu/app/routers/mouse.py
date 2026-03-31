"""
鼠标操作路由模块 - 提供鼠标点击、移动、拖拽、滚动等 API 接口。

接口列表：
- POST /api/mouse/click: 鼠标点击
- POST /api/mouse/move: 移动鼠标
- POST /api/mouse/drag: 鼠标拖拽
- POST /api/mouse/scroll: 鼠标滚动
- POST /api/mouse/down: 按下鼠标左键
- POST /api/mouse/up: 释放鼠标左键
"""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..tools import ToolError

# 创建鼠标操作路由，设置前缀和标签
router = APIRouter(prefix="/api/mouse", tags=["鼠标操作"])

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

class Coordinate(BaseModel):
    """坐标模型。"""
    x: int = Field(..., ge=0, description="X 坐标（像素）")
    y: int = Field(..., ge=0, description="Y 坐标（像素）")


class ClickRequest(BaseModel):
    """鼠标点击请求模型。"""
    button: Literal[
        "left_click", "right_click", "middle_click", "double_click", "triple_click"
    ] = Field(default="left_click", description="点击类型")
    coordinate: Coordinate | None = Field(default=None, description="点击位置坐标（不指定则在当前位置点击）")
    key: str | None = Field(default=None, description="同时按住的修饰键（如 ctrl、shift）")


class MoveRequest(BaseModel):
    """鼠标移动请求模型。"""
    coordinate: Coordinate = Field(..., description="目标坐标")


class DragRequest(BaseModel):
    """鼠标拖拽请求模型。"""
    start_coordinate: Coordinate = Field(..., description="拖拽起始坐标")
    end_coordinate: Coordinate = Field(..., description="拖拽结束坐标")


class ScrollRequest(BaseModel):
    """鼠标滚动请求模型。"""
    direction: Literal["up", "down", "left", "right"] = Field(..., description="滚动方向")
    amount: int = Field(..., ge=1, description="滚动量")
    coordinate: Coordinate | None = Field(default=None, description="滚动位置坐标（可选）")
    modifier_key: str | None = Field(default=None, description="同时按住的修饰键（可选）")


class MouseResponse(BaseModel):
    """鼠标操作响应模型。"""
    success: bool = Field(description="是否操作成功")
    output: str | None = Field(default=None, description="操作输出信息")
    error: str | None = Field(default=None, description="错误信息")
    base64_image: str | None = Field(default=None, description="操作后的截图（Base64 编码）")


# ==================== API 接口 ====================

@router.post("/click", response_model=MouseResponse, summary="鼠标点击")
async def mouse_click(request: ClickRequest):
    """执行鼠标点击操作。

    支持左键、右键、中键、双击和三击。
    可选择在指定坐标位置点击，或在当前位置点击。
    可同时按住修饰键（如 ctrl、shift）。

    参数:
        request: 点击操作的参数

    返回:
        MouseResponse: 包含操作结果和截图
    """
    try:
        tool = get_computer_tool()
        coord = (request.coordinate.x, request.coordinate.y) if request.coordinate else None
        result = await tool.mouse_click(
            button=request.button,
            coordinate=coord,
            key=request.key,
        )
        return MouseResponse(
            success=True,
            output=result.output,
            error=result.error,
            base64_image=result.base64_image,
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"鼠标点击失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"鼠标点击失败: {str(e)}")


@router.post("/move", response_model=MouseResponse, summary="移动鼠标")
async def mouse_move(request: MoveRequest):
    """移动鼠标到指定坐标位置。

    参数:
        request: 包含目标坐标的请求体

    返回:
        MouseResponse: 包含操作结果和截图
    """
    try:
        tool = get_computer_tool()
        result = await tool.mouse_move(
            coordinate=(request.coordinate.x, request.coordinate.y)
        )
        return MouseResponse(
            success=True,
            output=result.output,
            error=result.error,
            base64_image=result.base64_image,
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"鼠标移动失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"鼠标移动失败: {str(e)}")


@router.post("/drag", response_model=MouseResponse, summary="鼠标拖拽")
async def mouse_drag(request: DragRequest):
    """执行鼠标拖拽操作。

    从起始坐标按下鼠标左键，拖动到结束坐标后释放。

    参数:
        request: 包含起始和结束坐标的请求体

    返回:
        MouseResponse: 包含操作结果和截图
    """
    try:
        tool = get_computer_tool()
        result = await tool.mouse_drag(
            start_coordinate=(request.start_coordinate.x, request.start_coordinate.y),
            end_coordinate=(request.end_coordinate.x, request.end_coordinate.y),
        )
        return MouseResponse(
            success=True,
            output=result.output,
            error=result.error,
            base64_image=result.base64_image,
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"鼠标拖拽失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"鼠标拖拽失败: {str(e)}")


@router.post("/scroll", response_model=MouseResponse, summary="鼠标滚动")
async def mouse_scroll(request: ScrollRequest):
    """执行鼠标滚动操作。

    支持上下左右四个方向的滚动。
    可选择在指定位置滚动，并可同时按住修饰键。

    参数:
        request: 包含滚动方向、数量等参数的请求体

    返回:
        MouseResponse: 包含操作结果和截图
    """
    try:
        tool = get_computer_tool()
        coord = (request.coordinate.x, request.coordinate.y) if request.coordinate else None
        result = await tool.mouse_scroll(
            direction=request.direction,
            amount=request.amount,
            coordinate=coord,
            modifier_key=request.modifier_key,
        )
        return MouseResponse(
            success=True,
            output=result.output,
            error=result.error,
            base64_image=result.base64_image,
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"鼠标滚动失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"鼠标滚动失败: {str(e)}")


@router.post("/down", response_model=MouseResponse, summary="按下鼠标左键")
async def mouse_down():
    """按下鼠标左键（不释放）。

    用于实现自定义拖拽操作，需要配合 /api/mouse/up 使用。

    返回:
        MouseResponse: 包含操作结果和截图
    """
    try:
        tool = get_computer_tool()
        result = await tool.mouse_down()
        return MouseResponse(
            success=True,
            output=result.output,
            error=result.error,
            base64_image=result.base64_image,
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"按下鼠标失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"按下鼠标失败: {str(e)}")


@router.post("/up", response_model=MouseResponse, summary="释放鼠标左键")
async def mouse_up():
    """释放鼠标左键。

    用于实现自定义拖拽操作，需要配合 /api/mouse/down 使用。

    返回:
        MouseResponse: 包含操作结果和截图
    """
    try:
        tool = get_computer_tool()
        result = await tool.mouse_up()
        return MouseResponse(
            success=True,
            output=result.output,
            error=result.error,
            base64_image=result.base64_image,
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"释放鼠标失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"释放鼠标失败: {str(e)}")
