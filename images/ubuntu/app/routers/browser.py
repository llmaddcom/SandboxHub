"""
浏览器操作路由模块 - 提供浏览器控制的 API 接口。

接口列表：
- POST /api/browser/open_url: 用 Chrome 打开指定 URL
- POST /api/browser/close: 关闭浏览器
- GET  /api/browser/active_window: 获取当前活动窗口标题
"""

import asyncio
import shlex

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..tools import ToolError
from ..tools.run import run

router = APIRouter(prefix="/api/browser", tags=["浏览器操作"])

computer_tool = None


def get_computer_tool():
    if computer_tool is None:
        raise HTTPException(status_code=500, detail="电脑操作工具未初始化")
    return computer_tool


class OpenUrlRequest(BaseModel):
    url: str = Field(..., description="要打开的 URL 地址", examples=["https://www.google.com"])
    new_window: bool = Field(default=True, description="是否在新窗口中打开")


class BrowserResponse(BaseModel):
    success: bool = Field(description="是否操作成功")
    output: str | None = Field(default=None, description="操作输出信息")
    error: str | None = Field(default=None, description="错误信息")
    base64_image: str | None = Field(default=None, description="操作后的截图（Base64 编码）")


class ActiveWindowResponse(BaseModel):
    success: bool = Field(description="是否获取成功")
    window_id: str | None = Field(default=None, description="活动窗口 ID")
    window_title: str | None = Field(default=None, description="活动窗口标题")


@router.post("/open_url", response_model=BrowserResponse, summary="打开 URL")
async def open_url(request: OpenUrlRequest):
    """用 Chrome 浏览器打开指定 URL。

    启动 google-chrome-stable 并加载目标页面，等待页面加载后返回截图。
    """
    try:
        tool = get_computer_tool()
        flag = "--new-window" if request.new_window else "--new-tab"
        cmd = (
            f"DISPLAY=:{tool.display_num} nohup google-chrome-stable "
            f"--no-sandbox --disable-dev-shm-usage --disable-gpu "
            f"--remote-debugging-port=9222 "
            f"{flag} {shlex.quote(request.url)} > /dev/null 2>&1 &"
        )
        await run(cmd, timeout=10.0)
        await asyncio.sleep(3.0)
        screenshot = await tool.screenshot()
        return BrowserResponse(
            success=True,
            output=f"已打开 URL: {request.url}",
            base64_image=screenshot.base64_image,
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"浏览器操作失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"打开 URL 失败: {str(e)}")


@router.post("/close", response_model=BrowserResponse, summary="关闭浏览器")
async def close_browser():
    """关闭所有 Chrome 浏览器窗口。"""
    try:
        await run("pkill -f google-chrome-stable || true", timeout=10.0)
        await asyncio.sleep(1.0)
        tool = get_computer_tool()
        screenshot = await tool.screenshot()
        return BrowserResponse(
            success=True,
            output="已关闭所有浏览器窗口",
            base64_image=screenshot.base64_image,
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"关闭浏览器失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"关闭浏览器失败: {str(e)}")


@router.get("/active_window", response_model=ActiveWindowResponse, summary="获取活动窗口")
async def get_active_window():
    """获取当前活动窗口的 ID 和标题。"""
    try:
        tool = get_computer_tool()
        display_prefix = f"DISPLAY=:{tool.display_num} " if tool.display_num is not None else ""

        _, wid_out, _ = await run(f"{display_prefix}xdotool getactivewindow", timeout=5.0)
        window_id = wid_out.strip()

        _, title_out, _ = await run(
            f"{display_prefix}xdotool getactivewindow getwindowname", timeout=5.0
        )
        window_title = title_out.strip()

        return ActiveWindowResponse(
            success=True,
            window_id=window_id,
            window_title=window_title,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取活动窗口失败: {str(e)}")
