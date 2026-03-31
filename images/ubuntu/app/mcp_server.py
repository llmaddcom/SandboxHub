"""
FastMCP 服务入口 - 将沙盒操作能力以 MCP 协议暴露给 LLM。

为 LLM 操作电脑提供精炼的 MCP 工具集，直接调用底层工具类，
不经过 HTTP，性能更优、接口更清晰。

运行方式：python -m computer_use_demo.mcp_server
"""

import asyncio
import base64
import mimetypes
import os
import shlex
import signal
from pathlib import Path

import psutil
from fastmcp import FastMCP
from mcp.types import ImageContent, TextContent

from .routers.browser_cdp import _get_page as _cdp_get_page, _get_op_lock as _cdp_get_op_lock
from .tools import BashTool, ComputerTool, EditTool
from .tools.run import run

mcp = FastMCP("Ubuntu Sandbox MCP")

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff", ".tif"}


def _screenshot_result(base64_image: str | None, text: str) -> list[TextContent | ImageContent]:
    """将截图 base64 + 描述文本组合为多模态内容列表。"""
    blocks: list[TextContent | ImageContent] = [TextContent(type="text", text=text)]
    if base64_image:
        blocks.append(ImageContent(type="image", data=base64_image, mimeType="image/png"))
    return blocks

# ===== 工具实例（延迟初始化） =====
_computer: ComputerTool | None = None
_bash: BashTool | None = None
_edit: EditTool | None = None


def _get_computer() -> ComputerTool:
    global _computer
    if _computer is None:
        _computer = ComputerTool()
    return _computer


def _get_bash() -> BashTool:
    global _bash
    if _bash is None:
        _bash = BashTool()
    return _bash


def _get_edit() -> EditTool:
    global _edit
    if _edit is None:
        _edit = EditTool()
    return _edit


# ============================================================
# 屏幕操作
# ============================================================


@mcp.tool
async def take_screenshot() -> list[TextContent | ImageContent]:
    """截取当前屏幕完整截图。

    返回包含截图的图片内容，LLM 可以通过分析截图来理解屏幕上当前显示的内容。
    """
    computer = _get_computer()
    result = await computer.screenshot()
    return _screenshot_result(result.base64_image, "已截取当前屏幕截图")


@mcp.tool
async def get_screen_info() -> dict:
    """获取屏幕分辨率等信息。

    返回包含屏幕宽高、显示编号等信息的字典。
    """
    computer = _get_computer()
    return await computer.get_screen_info()


@mcp.tool
async def get_cursor_position() -> dict:
    """获取当前鼠标光标在屏幕上的坐标位置。"""
    computer = _get_computer()
    result = await computer.get_cursor_position()
    output = result.output or ""
    x = int(output.split("X=")[1].split(",")[0])
    y = int(output.split("Y=")[1])
    return {"x": x, "y": y}


@mcp.tool
async def zoom_screen(x0: int, y0: int, x1: int, y1: int) -> list[TextContent | ImageContent]:
    """放大查看屏幕指定矩形区域。

    裁剪屏幕截图的指定区域并返回，适用于查看界面细节。

    Args:
        x0: 左上角 X 坐标
        y0: 左上角 Y 坐标
        x1: 右下角 X 坐标
        y1: 右下角 Y 坐标
    """
    computer = _get_computer()
    result = await computer.zoom((x0, y0, x1, y1))
    return _screenshot_result(result.base64_image, f"已放大查看区域 ({x0},{y0})-({x1},{y1})")


@mcp.tool
async def wait_and_screenshot(seconds: float) -> list[TextContent | ImageContent]:
    """等待指定秒数后截图。

    适用于等待页面加载完成、动画结束等场景。

    Args:
        seconds: 等待时间（秒），范围 0-100
    """
    computer = _get_computer()
    result = await computer.wait(seconds)
    return _screenshot_result(result.base64_image, f"已等待 {seconds} 秒，返回了一张截图")


# ============================================================
# 鼠标操作
# ============================================================


@mcp.tool
async def click_at(x: int, y: int, button: str = "left_click") -> list[TextContent | ImageContent]:
    """在屏幕指定坐标位置点击鼠标。

    支持的点击类型：left_click, right_click, middle_click, double_click, triple_click。
    操作完成后返回屏幕截图，可用于验证点击效果。

    Args:
        x: 点击位置的 X 坐标（像素）
        y: 点击位置的 Y 坐标（像素）
        button: 点击类型，默认 left_click
    """
    computer = _get_computer()
    result = await computer.mouse_click(button=button, coordinate=(x, y))
    return _screenshot_result(result.base64_image, f"已在 ({x}, {y}) 执行 {button}，返回了一张截图")


@mcp.tool
async def move_mouse(x: int, y: int) -> list[TextContent | ImageContent]:
    """移动鼠标到屏幕指定坐标位置。

    Args:
        x: 目标 X 坐标（像素）
        y: 目标 Y 坐标（像素）
    """
    computer = _get_computer()
    result = await computer.mouse_move(coordinate=(x, y))
    return _screenshot_result(result.base64_image, f"已移动鼠标到 ({x}, {y})，返回了一张截图")


@mcp.tool
async def drag_mouse(start_x: int, start_y: int, end_x: int, end_y: int) -> list[TextContent | ImageContent]:
    """从起始坐标拖拽鼠标到目标坐标。

    按下左键从起始位置拖动到结束位置后释放。

    Args:
        start_x: 起始 X 坐标
        start_y: 起始 Y 坐标
        end_x: 结束 X 坐标
        end_y: 结束 Y 坐标
    """
    computer = _get_computer()
    result = await computer.mouse_drag(
        start_coordinate=(start_x, start_y),
        end_coordinate=(end_x, end_y),
    )
    return _screenshot_result(result.base64_image, f"已从 ({start_x},{start_y}) 拖拽到 ({end_x},{end_y})，返回了一张截图")


@mcp.tool
async def scroll(direction: str, amount: int = 3, x: int | None = None, y: int | None = None) -> list[TextContent | ImageContent]:
    """滚动鼠标滚轮。

    Args:
        direction: 滚动方向，可选 up/down/left/right
        amount: 滚动量，默认 3
        x: 滚动位置 X 坐标（可选，不指定则在当前位置滚动）
        y: 滚动位置 Y 坐标（可选）
    """
    computer = _get_computer()
    coord = (x, y) if x is not None and y is not None else None
    result = await computer.mouse_scroll(direction=direction, amount=amount, coordinate=coord)
    return _screenshot_result(result.base64_image, f"已向 {direction} 滚动 {amount} 格，返回了一张截图")


# ============================================================
# 键盘操作
# ============================================================


@mcp.tool
async def type_text(text: str) -> list[TextContent | ImageContent]:
    """模拟键盘输入文本。

    将文本逐字输入到当前焦点位置，适用于在文本框、编辑器、终端等输入文字。

    Args:
        text: 要输入的文本内容
    """
    computer = _get_computer()
    result = await computer.type_text(text)
    return _screenshot_result(result.base64_image, f"已输入文本，返回了一张截图")


@mcp.tool
async def press_key(key: str) -> list[TextContent | ImageContent]:
    """按下键盘快捷键或单个按键。

    支持单个按键（如 Return, Tab, Escape）和组合键（如 ctrl+c, alt+F4, ctrl+shift+t）。
    按键名称遵循 xdotool 格式。

    Args:
        key: 按键名称或组合键，例如 'Return', 'ctrl+c', 'alt+F4', 'super'
    """
    computer = _get_computer()
    result = await computer.key_press(key)
    return _screenshot_result(result.base64_image, f"已按下 {key}，返回了一张截图")


# ============================================================
# 终端/Shell
# ============================================================


@mcp.tool
async def execute_shell(command: str) -> dict:
    """在终端中执行一条 bash 命令。

    Args:
        command: 要执行的 bash 命令，例如 'ls -la' 或 'echo hello'

    Returns:
        包含 output(标准输出) 和 error(错误输出) 的字典
    """
    bash = _get_bash()
    result = await bash.execute(command)
    return {
        "output": result.output or "",
        "error": result.error or "",
        "system": result.system or "",
    }


# ============================================================
# 文件操作
# ============================================================


@mcp.tool
async def read_file(path: str, line_start: int | None = None, line_end: int | None = None) -> str:
    """读取文件内容。

    返回带行号的文件内容。对于目录则返回文件列表。

    Args:
        path: 文件或目录的绝对路径
        line_start: 起始行号（可选，从 1 开始）
        line_end: 结束行号（可选）
    """
    edit = _get_edit()
    view_range = [line_start, line_end] if line_start is not None and line_end is not None else None
    result = await edit.view(path, view_range)
    return result.output or result.error or ""


@mcp.tool
async def write_file(path: str, content: str) -> str:
    """创建新文件并写入内容。

    如果文件已存在则操作失败。使用 edit_file 来修改已有文件。

    Args:
        path: 新文件的绝对路径
        content: 文件内容
    """
    edit = _get_edit()
    result = await edit.create(path, content)
    return result.output or result.error or ""


@mcp.tool
async def edit_file(path: str, old_text: str, new_text: str) -> str:
    """通过字符串替换编辑文件。

    在文件中查找 old_text 并替换为 new_text。old_text 必须在文件中唯一。

    Args:
        path: 文件的绝对路径
        old_text: 要被替换的原始文本（必须在文件中唯一出现）
        new_text: 替换后的新文本
    """
    edit = _get_edit()
    result = await edit.str_replace(path, old_text, new_text)
    return result.output or result.error or ""


@mcp.tool
async def upload_file(filename: str, base64_content: str, dest_dir: str = "/home/computeruse") -> str:
    """上传文件到沙盒（通过 base64 编码内容）。

    将 base64 编码的文件内容解码后保存到沙盒指定目录。
    自动创建不存在的中间目录。

    Args:
        filename: 文件名（如 'report.pdf'）
        base64_content: 文件内容的 base64 编码字符串
        dest_dir: 目标目录的绝对路径，默认 /home/computeruse
    """
    target_dir = Path(dest_dir)
    if ".." in Path(dest_dir).parts:
        return "错误: 路径中不允许包含 '..'"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / filename

    try:
        content = base64.b64decode(base64_content)
    except Exception:
        return "错误: base64 内容解码失败"

    target_path.write_bytes(content)
    return f"文件已保存到 {target_path}（{len(content)} 字节）"


@mcp.tool
async def download_file(path: str) -> list[TextContent | ImageContent]:
    """从沙盒下载文件。

    读取沙盒内的文件。图片文件直接返回图片内容，其他文件返回 base64 编码。

    Args:
        path: 文件的绝对路径
    """
    file_path = Path(path)
    if ".." in Path(path).parts:
        return [TextContent(type="text", text="错误: 路径中不允许包含 '..'")]
    if not file_path.exists():
        return [TextContent(type="text", text=f"错误: 文件不存在: {path}")]
    if not file_path.is_file():
        return [TextContent(type="text", text=f"错误: 路径不是文件: {path}")]

    content = file_path.read_bytes()
    suffix = file_path.suffix.lower()

    if suffix in IMAGE_EXTENSIONS:
        mime_type = mimetypes.guess_type(file_path.name)[0] or f"image/{suffix.lstrip('.')}"
        return [
            TextContent(type="text", text=f"返回了一张图片: {file_path.name} ({len(content)} 字节)"),
            ImageContent(type="image", data=base64.b64encode(content).decode("ascii"), mimeType=mime_type),
        ]

    return [
        TextContent(
            type="text",
            text=(
                f"文件: {file_path.name} ({len(content)} 字节)\n"
                f"base64_content: {base64.b64encode(content).decode('ascii')}"
            ),
        )
    ]


# ============================================================
# 浏览器操作
# ============================================================


@mcp.tool
async def open_browser(url: str) -> list[TextContent | ImageContent]:
    """用 Chrome 浏览器打开指定 URL。

    启动 google-chrome-stable 加载页面，等待 3 秒后返回截图。

    Args:
        url: 要打开的网址，例如 'https://www.google.com'
    """
    computer = _get_computer()
    display_num = computer.display_num
    cmd = (
        f"DISPLAY=:{display_num} nohup google-chrome-stable "
        f"--no-sandbox --disable-dev-shm-usage --disable-gpu "
        f"--remote-debugging-port=9222 "
        f"--new-window {shlex.quote(url)} > /dev/null 2>&1 &"
    )
    await run(cmd, timeout=10.0)
    await asyncio.sleep(3.0)
    result = await computer.screenshot()
    return _screenshot_result(result.base64_image, f"已打开浏览器访问 {url}，返回了一张截图")


@mcp.tool
async def close_browser() -> list[TextContent | ImageContent]:
    """关闭所有 Chrome 浏览器窗口。"""
    await run("pkill -f google-chrome-stable || true", timeout=10.0)
    await asyncio.sleep(1.0)
    computer = _get_computer()
    result = await computer.screenshot()
    return _screenshot_result(result.base64_image, "已关闭所有浏览器窗口，返回了一张截图")


# ============================================================
# 进程与窗口管理
# ============================================================


@mcp.tool
async def list_processes() -> list[dict]:
    """列出当前运行中的所有进程。

    返回进程列表，每个进程包含 pid、name、cpu_percent、memory_mb、status 信息。
    """
    processes = []
    for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "status"]):
        try:
            info = proc.info
            mem = info.get("memory_info")
            processes.append({
                "pid": info["pid"],
                "name": info["name"] or "",
                "cpu_percent": info.get("cpu_percent"),
                "memory_mb": round(mem.rss / 1024 / 1024, 2) if mem else None,
                "status": info.get("status", "unknown"),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return processes


@mcp.tool
async def kill_process(pid: int, force: bool = False) -> str:
    """终止指定 PID 的进程。

    Args:
        pid: 进程 ID
        force: 是否强制终止（SIGKILL），默认发送 SIGTERM
    """
    sig = signal.SIGKILL if force else signal.SIGTERM
    os.kill(pid, sig)
    return f"已发送 {'SIGKILL' if force else 'SIGTERM'} 到 PID {pid}"


@mcp.tool
async def list_windows() -> list[dict]:
    """列出桌面上所有可见窗口，返回窗口 ID 和标题。"""
    computer = _get_computer()
    display_prefix = f"DISPLAY=:{computer.display_num} " if computer.display_num is not None else ""

    _, stdout, _ = await run(f"{display_prefix}xdotool search --onlyvisible --name ''", timeout=5.0)
    window_ids = [wid.strip() for wid in stdout.strip().split("\n") if wid.strip()]

    windows = []
    for wid in window_ids:
        _, title_out, _ = await run(f"{display_prefix}xdotool getwindowname {wid}", timeout=3.0)
        title = title_out.strip()
        if title:
            windows.append({"window_id": wid, "title": title})
    return windows


@mcp.tool
async def focus_window(window_id: str) -> list[TextContent | ImageContent]:
    """激活（聚焦）指定窗口。

    Args:
        window_id: 窗口 ID（可从 list_windows 获取）
    """
    computer = _get_computer()
    display_prefix = f"DISPLAY=:{computer.display_num} " if computer.display_num is not None else ""
    await run(f"{display_prefix}xdotool windowactivate {window_id}", timeout=5.0)
    result = await computer.screenshot()
    return _screenshot_result(result.base64_image, f"已聚焦窗口 {window_id}，返回了一张截图")


@mcp.tool
async def close_window(window_id: str) -> list[TextContent | ImageContent]:
    """关闭指定窗口。

    Args:
        window_id: 窗口 ID（可从 list_windows 获取）
    """
    computer = _get_computer()
    display_prefix = f"DISPLAY=:{computer.display_num} " if computer.display_num is not None else ""
    await run(f"{display_prefix}xdotool windowclose {window_id}", timeout=5.0)
    result = await computer.screenshot()
    return _screenshot_result(result.base64_image, f"已关闭窗口 {window_id}，返回了一张截图")


@mcp.tool
async def get_active_window() -> dict:
    """获取当前活动窗口的 ID 和标题。"""
    computer = _get_computer()
    display_prefix = f"DISPLAY=:{computer.display_num} " if computer.display_num is not None else ""

    _, wid_out, _ = await run(f"{display_prefix}xdotool getactivewindow", timeout=5.0)
    _, title_out, _ = await run(f"{display_prefix}xdotool getactivewindow getwindowname", timeout=5.0)
    return {
        "window_id": wid_out.strip(),
        "window_title": title_out.strip(),
    }


# ============================================================
# 剪贴板与系统信息
# ============================================================


@mcp.tool
async def get_clipboard() -> str:
    """获取系统剪贴板的文本内容。"""
    computer = _get_computer()
    display_prefix = f"DISPLAY=:{computer.display_num} " if computer.display_num is not None else ""
    _, stdout, _ = await run(f"{display_prefix}xclip -selection clipboard -o", timeout=5.0)
    return stdout


@mcp.tool
async def set_clipboard(content: str) -> str:
    """设置系统剪贴板的文本内容。

    Args:
        content: 要复制到剪贴板的文本
    """
    computer = _get_computer()
    display_prefix = f"DISPLAY=:{computer.display_num} " if computer.display_num is not None else ""
    escaped = shlex.quote(content)
    await run(f"echo -n {escaped} | {display_prefix}xclip -selection clipboard", timeout=5.0)
    return "剪贴板内容已更新"


@mcp.tool
async def get_system_info() -> dict:
    """获取系统资源使用信息（CPU、内存、磁盘）。"""
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return {
        "cpu_count": psutil.cpu_count() or 1,
        "cpu_percent": psutil.cpu_percent(interval=0.5),
        "memory_total_mb": round(mem.total / 1024 / 1024, 2),
        "memory_used_mb": round(mem.used / 1024 / 1024, 2),
        "memory_percent": mem.percent,
        "disk_total_gb": round(disk.total / 1024 / 1024 / 1024, 2),
        "disk_used_gb": round(disk.used / 1024 / 1024 / 1024, 2),
        "disk_percent": disk.percent,
    }


# ============================================================
# CDP 浏览器精控（通过 playwright connect_over_cdp）
# ============================================================


@mcp.tool
async def cdp_navigate(url: str) -> dict:
    """通过 CDP 导航 Chrome 到指定 URL。

    等待 DOM 加载完成后返回页面标题和最终 URL。

    Args:
        url: 要导航到的网址
    """
    async with _cdp_get_op_lock():
        page = await _cdp_get_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return {"url": page.url, "title": await page.title()}


@mcp.tool
async def cdp_get_text(selector: str | None = None) -> str:
    """通过 CDP 获取页面或元素的文本内容。

    Args:
        selector: CSS 选择器（可选），None 则返回整页 body 文本
    """
    async with _cdp_get_op_lock():
        page = await _cdp_get_page()
        if selector:
            return await page.locator(selector).first.inner_text(timeout=5000)
        return await page.inner_text("body", timeout=10000)


@mcp.tool
async def cdp_click_selector(selector: str) -> str:
    """通过 CDP 点击指定 CSS 选择器对应的元素。

    Args:
        selector: CSS 选择器，例如 'button#submit'
    """
    async with _cdp_get_op_lock():
        page = await _cdp_get_page()
        await page.locator(selector).first.click(timeout=5000)
        return f"已点击 {selector}"


@mcp.tool
async def cdp_fill_input(selector: str, value: str) -> str:
    """通过 CDP 清空并填写指定输入框。

    Args:
        selector: 输入框的 CSS 选择器
        value: 要填入的文本值
    """
    async with _cdp_get_op_lock():
        page = await _cdp_get_page()
        locator = page.locator(selector).first
        await locator.clear(timeout=5000)
        await locator.fill(value, timeout=5000)
        return f"已填写 {selector}"


@mcp.tool
async def cdp_evaluate(script: str) -> str:
    """通过 CDP 在当前页面执行 JavaScript 并返回结果。

    Args:
        script: 要执行的 JavaScript 代码，例如 'document.title'
    """
    async with _cdp_get_op_lock():
        page = await _cdp_get_page()
        result = await page.evaluate(script)
        return str(result)


@mcp.tool
async def cdp_get_url() -> dict:
    """通过 CDP 获取当前页面的 URL 和标题。"""
    async with _cdp_get_op_lock():
        page = await _cdp_get_page()
        return {"url": page.url, "title": await page.title()}


@mcp.tool
async def cdp_get_html() -> str:
    """通过 CDP 获取当前页面的完整 HTML 内容。"""
    async with _cdp_get_op_lock():
        page = await _cdp_get_page()
        return await page.content()


@mcp.tool
async def cdp_scroll(
    direction: str,
    amount: int = 300,
    selector: str | None = None,
) -> str:
    """通过 CDP 滚动页面或指定元素。

    Args:
        direction: 滚动方向，可选 up/down/left/right
        amount: 滚动像素量，默认 300
        selector: 要滚动的元素 CSS 选择器（可选，None 则滚动整个页面）
    """
    async with _cdp_get_op_lock():
        page = await _cdp_get_page()
        dx, dy = 0, 0
        if direction == "down":
            dy = amount
        elif direction == "up":
            dy = -amount
        elif direction == "right":
            dx = amount
        elif direction == "left":
            dx = -amount
        else:
            return f"无效的滚动方向: {direction}，支持 up/down/left/right"

        if selector:
            await page.locator(selector).first.scroll_into_view_if_needed(timeout=5000)
            await page.locator(selector).first.evaluate(f"el => el.scrollBy({dx}, {dy})")
        else:
            await page.evaluate(f"window.scrollBy({dx}, {dy})")
        return f"已向 {direction} 滚动 {amount}px"


@mcp.tool
async def cdp_hover(selector: str) -> str:
    """通过 CDP 将鼠标悬停到指定 CSS 选择器对应的元素上。

    Args:
        selector: 要悬停的元素 CSS 选择器
    """
    async with _cdp_get_op_lock():
        page = await _cdp_get_page()
        await page.locator(selector).first.hover(timeout=5000)
        return f"已悬停到 {selector}"


@mcp.tool
async def cdp_wait_for_selector(
    selector: str,
    timeout_ms: int = 5000,
    state: str = "visible",
) -> str:
    """通过 CDP 等待指定 CSS 选择器的元素达到目标状态。

    Args:
        selector: 等待出现的元素 CSS 选择器
        timeout_ms: 等待超时（毫秒），默认 5000
        state: 等待状态，可选 visible/hidden/attached/detached，默认 visible
    """
    async with _cdp_get_op_lock():
        page = await _cdp_get_page()
        await page.wait_for_selector(selector, timeout=timeout_ms, state=state)  # type: ignore[arg-type]
        return f"元素 {selector} 已达到状态 {state}"


if __name__ == "__main__":
    mcp.run(transport="sse", host="0.0.0.0", port=8001)
