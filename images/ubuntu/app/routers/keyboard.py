"""
键盘操作路由模块 - 提供键盘按键和文本输入的 API 接口。

接口列表：
- POST /api/keyboard/key: 按下快捷键组合
- POST /api/keyboard/type: 输入文本
- POST /api/keyboard/hold_key: 长按按键
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from computer_use_demo.tools import ToolError

# 创建键盘操作路由，设置前缀和标签
router = APIRouter(prefix="/api/keyboard", tags=["键盘操作"])

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

class KeyPressRequest(BaseModel):
    """按键请求模型。"""
    key: str = Field(
        ...,
        description="按键名称或组合键（如 'Return'、'ctrl+c'、'alt+F4'、'super'）",
        examples=["Return", "ctrl+c", "alt+F4", "Tab"],
    )


class TypeTextRequest(BaseModel):
    """文本输入请求模型。"""
    text: str = Field(
        ...,
        description="要输入的文本内容",
        examples=["Hello World", "https://www.google.com"],
    )


class HoldKeyRequest(BaseModel):
    """长按按键请求模型。"""
    key: str = Field(
        ...,
        description="要长按的按键名称",
        examples=["shift", "ctrl", "alt"],
    )
    duration: float = Field(
        ...,
        gt=0,
        le=100,
        description="按住时间（秒），最大 100 秒",
        examples=[1.0, 2.5],
    )


class KeyboardResponse(BaseModel):
    """键盘操作响应模型。"""
    success: bool = Field(description="是否操作成功")
    output: str | None = Field(default=None, description="操作输出信息")
    error: str | None = Field(default=None, description="错误信息")
    base64_image: str | None = Field(default=None, description="操作后的截图（Base64 编码）")


# ==================== API 接口 ====================

@router.post("/key", response_model=KeyboardResponse, summary="按下快捷键")
async def press_key(request: KeyPressRequest):
    """按下指定的键或组合键。

    支持单个按键（如 Return、Tab、Escape）和组合键（如 ctrl+c、alt+F4）。
    使用 xdotool 的按键名称格式。

    参数:
        request: 包含按键名称的请求体

    返回:
        KeyboardResponse: 包含操作结果和截图
    """
    try:
        tool = get_computer_tool()
        result = await tool.key_press(request.key)
        return KeyboardResponse(
            success=True,
            output=result.output,
            error=result.error,
            base64_image=result.base64_image,
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"按键失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"按键失败: {str(e)}")


@router.post("/type", response_model=KeyboardResponse, summary="输入文本")
async def type_text(request: TypeTextRequest):
    """模拟键盘输入文本。

    将文本分成小块依次输入，模拟真实键盘输入速度。
    输入完成后自动截取屏幕截图。

    参数:
        request: 包含要输入文本的请求体

    返回:
        KeyboardResponse: 包含操作结果和截图
    """
    try:
        tool = get_computer_tool()
        result = await tool.type_text(request.text)
        return KeyboardResponse(
            success=True,
            output=result.output,
            error=result.error,
            base64_image=result.base64_image,
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"文本输入失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文本输入失败: {str(e)}")


@router.post("/hold_key", response_model=KeyboardResponse, summary="长按按键")
async def hold_key(request: HoldKeyRequest):
    """长按指定按键一段时间。

    按下按键后保持指定时间，然后释放。
    适用于需要长按操作的场景。

    参数:
        request: 包含按键名称和按住时长的请求体

    返回:
        KeyboardResponse: 包含操作结果和截图
    """
    try:
        tool = get_computer_tool()
        result = await tool.hold_key(request.key, request.duration)
        return KeyboardResponse(
            success=True,
            output=result.output,
            error=result.error,
            base64_image=result.base64_image,
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"长按按键失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"长按按键失败: {str(e)}")
