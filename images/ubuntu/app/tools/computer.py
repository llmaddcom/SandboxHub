"""
电脑操作工具模块 - 提供屏幕、鼠标、键盘的底层操作功能。

本模块实现了对虚拟桌面环境的各种操作，包括：
- 鼠标操作：点击、移动、拖拽、滚动
- 键盘操作：按键、输入文本、长按
- 屏幕操作：截图、获取光标位置、区域缩放
- 坐标缩放：在 API 坐标和实际屏幕坐标之间转换
"""

import asyncio
import base64
import os
import shlex
import shutil
from enum import StrEnum
from pathlib import Path
from typing import Literal, TypedDict
from uuid import uuid4

from .base import ToolError, ToolResult
from .run import run

# 截图输出目录
OUTPUT_DIR = "/tmp/outputs"

# 键盘输入延迟（毫秒）和分组大小
TYPING_DELAY_MS = 12
TYPING_GROUP_SIZE = 50

# 支持的鼠标点击类型及其对应的 xdotool 按钮参数
CLICK_BUTTONS = {
    "left_click": 1,
    "right_click": 3,
    "middle_click": 2,
    "double_click": "--repeat 2 --delay 10 1",
    "triple_click": "--repeat 3 --delay 10 1",
}

# 鼠标滚动方向对应的 X11 按钮编号
SCROLL_BUTTONS = {
    "up": 4,
    "down": 5,
    "left": 6,
    "right": 7,
}

# 滚动方向类型
ScrollDirection = Literal["up", "down", "left", "right"]


class Resolution(TypedDict):
    """屏幕分辨率类型定义。"""
    width: int
    height: int


# 最大缩放目标分辨率（超过这些分辨率时会进行缩放）
MAX_SCALING_TARGETS: dict[str, Resolution] = {
    "XGA": Resolution(width=1024, height=768),      # 4:3 比例
    "WXGA": Resolution(width=1280, height=800),     # 16:10 比例
    "FWXGA": Resolution(width=1366, height=768),    # ~16:9 比例
}


class ScalingSource(StrEnum):
    """坐标缩放来源枚举。

    COMPUTER: 从屏幕坐标缩放到 API 坐标（缩小）
    API: 从 API 坐标缩放到屏幕坐标（放大）
    """
    COMPUTER = "computer"
    API = "api"


def chunks(s: str, chunk_size: int) -> list[str]:
    """将字符串按指定大小分割为多个块。

    参数:
        s: 要分割的字符串
        chunk_size: 每个块的大小

    返回:
        分割后的字符串列表
    """
    return [s[i : i + chunk_size] for i in range(0, len(s), chunk_size)]


class ComputerTool:
    """电脑操作工具类。

    提供对虚拟桌面环境的完整操作能力，包括鼠标、键盘和屏幕操作。
    通过 xdotool 和 scrot/gnome-screenshot 等工具实现底层操作。

    属性:
        width: 屏幕宽度（像素）
        height: 屏幕高度（像素）
        display_num: X11 显示编号
        _screenshot_delay: 截图前的等待延迟（秒）
        _scaling_enabled: 是否启用坐标缩放
    """

    width: int
    height: int
    display_num: int | None

    _screenshot_delay = 0.5  # 截图前等待时间（秒）
    _scaling_enabled = True  # 是否启用坐标缩放

    def __init__(self):
        """初始化电脑操作工具。

        从环境变量读取屏幕尺寸和显示编号。

        抛出:
            AssertionError: 如果 WIDTH 或 HEIGHT 环境变量未设置
        """
        self.width = int(os.getenv("WIDTH") or 0)
        self.height = int(os.getenv("HEIGHT") or 0)
        assert self.width and self.height, "必须设置 WIDTH 和 HEIGHT 环境变量"

        # 设置 X11 显示前缀
        if (display_num := os.getenv("DISPLAY_NUM")) is not None:
            self.display_num = int(display_num)
            self._display_prefix = f"DISPLAY=:{self.display_num} "
        else:
            self.display_num = None
            self._display_prefix = ""

        # xdotool 命令前缀（包含显示设置）
        self.xdotool = f"{self._display_prefix}xdotool"

    # ==================== 屏幕操作 ====================

    async def screenshot(self) -> ToolResult:
        """截取当前屏幕的截图。

        优先使用 gnome-screenshot，如不可用则使用 scrot。
        如果启用了缩放，会将截图调整到目标分辨率。

        返回:
            ToolResult: 包含 base64 编码截图的结果

        抛出:
            ToolError: 如果截图失败
        """
        output_dir = Path(OUTPUT_DIR)
        await asyncio.to_thread(output_dir.mkdir, parents=True, exist_ok=True)
        path = output_dir / f"screenshot_{uuid4().hex}.png"

        # 优先使用 gnome-screenshot，否则使用 scrot
        if shutil.which("gnome-screenshot"):
            screenshot_cmd = f"{self._display_prefix}gnome-screenshot -f {path} -p"
        else:
            screenshot_cmd = f"{self._display_prefix}scrot -p {path}"

        result = await self.shell(screenshot_cmd, take_screenshot=False)

        # 如果启用缩放，调整截图尺寸
        if self._scaling_enabled:
            x, y = self.scale_coordinates(
                ScalingSource.COMPUTER, self.width, self.height
            )
            await self.shell(
                f"convert {path} -resize {x}x{y}! {path}", take_screenshot=False
            )

        if path.exists():
            return result.replace(
                base64_image=base64.b64encode(path.read_bytes()).decode()
            )
        raise ToolError(f"截图失败: {result.error}")

    async def get_cursor_position(self) -> ToolResult:
        """获取当前鼠标光标位置。

        返回:
            ToolResult: 包含格式为 "X=数值,Y=数值" 的光标坐标
        """
        command_parts = [self.xdotool, "getmouselocation --shell"]
        result = await self.shell(
            " ".join(command_parts),
            take_screenshot=False,
        )
        output = result.output or ""
        # 解析 xdotool 输出的坐标并进行缩放
        x, y = self.scale_coordinates(
            ScalingSource.COMPUTER,
            int(output.split("X=")[1].split("\n")[0]),
            int(output.split("Y=")[1].split("\n")[0]),
        )
        return result.replace(output=f"X={x},Y={y}")

    async def get_screen_info(self) -> dict:
        """获取屏幕信息。

        返回:
            dict: 包含屏幕宽度、高度和显示编号的字典
        """
        width, height = self.scale_coordinates(
            ScalingSource.COMPUTER, self.width, self.height
        )
        return {
            "display_width_px": width,
            "display_height_px": height,
            "display_number": self.display_num,
            "actual_width_px": self.width,
            "actual_height_px": self.height,
        }

    async def zoom(self, region: tuple[int, int, int, int]) -> ToolResult:
        """缩放查看屏幕指定区域。

        对屏幕进行截图，然后裁剪到指定区域。

        参数:
            region: 区域坐标 (x0, y0, x1, y1)

        返回:
            ToolResult: 包含裁剪后 base64 截图的结果

        抛出:
            ToolError: 如果参数无效或操作失败
        """
        if (
            region is None
            or not isinstance(region, (list, tuple))
            or len(region) != 4
        ):
            raise ToolError(f"{region=} 必须是包含 4 个坐标的元组 (x0, y0, x1, y1)")
        if not all(isinstance(c, int) and c >= 0 for c in region):
            raise ToolError(f"{region=} 必须包含非负整数")

        x0, y0, x1, y1 = region
        # 将 API 坐标转换为屏幕坐标
        x0, y0 = self.scale_coordinates(ScalingSource.API, x0, y0)
        x1, y1 = self.scale_coordinates(ScalingSource.API, x1, y1)

        # 先截取完整屏幕
        screenshot_result = await self.screenshot()
        if not screenshot_result.base64_image:
            raise ToolError("截图失败，无法进行缩放")

        # 使用 ImageMagick 裁剪指定区域
        output_dir = Path(OUTPUT_DIR)
        temp_path = output_dir / f"screenshot_{uuid4().hex}.png"
        cropped_path = output_dir / f"zoomed_{uuid4().hex}.png"

        # 将截图写入临时文件
        temp_path.write_bytes(base64.b64decode(screenshot_result.base64_image))

        # 使用 ImageMagick convert 裁剪
        width = x1 - x0
        height = y1 - y0
        crop_cmd = f"convert {temp_path} -crop {width}x{height}+{x0}+{y0} +repage {cropped_path}"
        await run(crop_cmd)

        if cropped_path.exists():
            cropped_base64 = base64.b64encode(cropped_path.read_bytes()).decode()
            # 清理临时文件
            temp_path.unlink(missing_ok=True)
            cropped_path.unlink(missing_ok=True)
            return ToolResult(base64_image=cropped_base64)

        raise ToolError("裁剪截图失败")

    # ==================== 鼠标操作 ====================

    async def mouse_click(
        self,
        button: str = "left_click",
        coordinate: tuple[int, int] | None = None,
        key: str | None = None,
    ) -> ToolResult:
        """执行鼠标点击操作。

        参数:
            button: 点击类型（left_click/right_click/middle_click/double_click/triple_click）
            coordinate: 点击位置坐标 [x, y]（可选，不指定则在当前位置点击）
            key: 同时按住的修饰键（可选，如 "ctrl"、"shift"）

        返回:
            ToolResult: 包含操作结果和截图的结果

        抛出:
            ToolError: 如果点击类型无效
        """
        if button not in CLICK_BUTTONS:
            raise ToolError(f"无效的点击类型: {button}，支持: {list(CLICK_BUTTONS.keys())}")

        mouse_move_part = ""
        if coordinate is not None:
            x, y = self.validate_and_get_coordinates(coordinate)
            mouse_move_part = f"mousemove --sync {x} {y}"

        command_parts = [self.xdotool, mouse_move_part]
        # 如果需要同时按住修饰键
        if key:
            command_parts.append(f"keydown {key}")
        command_parts.append(f"click {CLICK_BUTTONS[button]}")
        if key:
            command_parts.append(f"keyup {key}")

        return await self.shell(" ".join(command_parts))

    async def mouse_move(self, coordinate: tuple[int, int]) -> ToolResult:
        """移动鼠标到指定坐标。

        参数:
            coordinate: 目标坐标 [x, y]

        返回:
            ToolResult: 包含操作结果和截图的结果
        """
        x, y = self.validate_and_get_coordinates(coordinate)
        command_parts = [self.xdotool, f"mousemove --sync {x} {y}"]
        return await self.shell(" ".join(command_parts))

    async def mouse_drag(
        self,
        start_coordinate: tuple[int, int],
        end_coordinate: tuple[int, int],
    ) -> ToolResult:
        """执行鼠标拖拽操作。

        参数:
            start_coordinate: 拖拽起始坐标 [x, y]
            end_coordinate: 拖拽结束坐标 [x, y]

        返回:
            ToolResult: 包含操作结果和截图的结果
        """
        start_x, start_y = self.validate_and_get_coordinates(start_coordinate)
        end_x, end_y = self.validate_and_get_coordinates(end_coordinate)
        command_parts = [
            self.xdotool,
            f"mousemove --sync {start_x} {start_y} mousedown 1 mousemove --sync {end_x} {end_y} mouseup 1",
        ]
        return await self.shell(" ".join(command_parts))

    async def mouse_scroll(
        self,
        direction: ScrollDirection,
        amount: int,
        coordinate: tuple[int, int] | None = None,
        modifier_key: str | None = None,
    ) -> ToolResult:
        """执行鼠标滚动操作。

        参数:
            direction: 滚动方向（up/down/left/right）
            amount: 滚动量
            coordinate: 滚动位置坐标（可选）
            modifier_key: 同时按住的修饰键（可选）

        返回:
            ToolResult: 包含操作结果和截图的结果

        抛出:
            ToolError: 如果参数无效
        """
        if direction not in SCROLL_BUTTONS:
            raise ToolError(f"{direction=} 必须是 'up'、'down'、'left' 或 'right'")
        if not isinstance(amount, int) or amount < 0:
            raise ToolError(f"{amount=} 必须是非负整数")

        mouse_move_part = ""
        if coordinate is not None:
            x, y = self.validate_and_get_coordinates(coordinate)
            mouse_move_part = f"mousemove --sync {x} {y}"

        scroll_button = SCROLL_BUTTONS[direction]

        command_parts = [self.xdotool, mouse_move_part]
        if modifier_key:
            command_parts.append(f"keydown {modifier_key}")
        command_parts.append(f"click --repeat {amount} {scroll_button}")
        if modifier_key:
            command_parts.append(f"keyup {modifier_key}")

        return await self.shell(" ".join(command_parts))

    async def mouse_down(self) -> ToolResult:
        """按下鼠标左键。

        返回:
            ToolResult: 包含操作结果和截图的结果
        """
        command_parts = [self.xdotool, "mousedown 1"]
        return await self.shell(" ".join(command_parts))

    async def mouse_up(self) -> ToolResult:
        """释放鼠标左键。

        返回:
            ToolResult: 包含操作结果和截图的结果
        """
        command_parts = [self.xdotool, "mouseup 1"]
        return await self.shell(" ".join(command_parts))

    # ==================== 键盘操作 ====================

    async def key_press(self, key: str) -> ToolResult:
        """按下指定的键或组合键。

        参数:
            key: 按键名称或组合键（如 "Return"、"ctrl+c"、"alt+F4"）

        返回:
            ToolResult: 包含操作结果和截图的结果

        抛出:
            ToolError: 如果未提供按键
        """
        if not key:
            raise ToolError("必须提供按键名称")
        command_parts = [self.xdotool, f"key -- {key}"]
        return await self.shell(" ".join(command_parts))

    async def type_text(self, text: str) -> ToolResult:
        """输入文本内容。

        将文本分成小块依次输入，以避免输入过快导致丢字。
        输入完成后自动截图。

        参数:
            text: 要输入的文本

        返回:
            ToolResult: 包含输入结果和截图的结果

        抛出:
            ToolError: 如果未提供文本
        """
        if not text:
            raise ToolError("必须提供要输入的文本")
        if not isinstance(text, str):
            raise ToolError(f"{text} 必须是字符串")

        results: list[ToolResult] = []
        # 将文本分块输入，避免过快导致丢字
        for chunk in chunks(text, TYPING_GROUP_SIZE):
            command_parts = [
                self.xdotool,
                f"type --delay {TYPING_DELAY_MS} -- {shlex.quote(chunk)}",
            ]
            results.append(
                await self.shell(" ".join(command_parts), take_screenshot=False)
            )
        # 输入完成后截图
        screenshot_base64 = (await self.screenshot()).base64_image
        return ToolResult(
            output="".join(result.output or "" for result in results),
            error="".join(result.error or "" for result in results),
            base64_image=screenshot_base64,
        )

    async def hold_key(self, key: str, duration: float) -> ToolResult:
        """长按指定按键。

        参数:
            key: 要长按的按键名称
            duration: 按住时间（秒）

        返回:
            ToolResult: 包含操作结果和截图的结果

        抛出:
            ToolError: 如果参数无效
        """
        if not key:
            raise ToolError("必须提供按键名称")
        if duration is None or not isinstance(duration, (int, float)):
            raise ToolError(f"{duration=} 必须是数字")
        if duration < 0:
            raise ToolError(f"{duration=} 必须是非负数")
        if duration > 100:
            raise ToolError(f"{duration=} 时间过长")

        escaped_keys = shlex.quote(key)
        command_parts = [
            self.xdotool,
            f"keydown {escaped_keys}",
            f"sleep {duration}",
            f"keyup {escaped_keys}",
        ]
        return await self.shell(" ".join(command_parts))

    # ==================== 系统操作 ====================

    async def wait(self, duration: float) -> ToolResult:
        """等待指定时间后截图。

        参数:
            duration: 等待时间（秒）

        返回:
            ToolResult: 包含截图的结果

        抛出:
            ToolError: 如果参数无效
        """
        if duration is None or not isinstance(duration, (int, float)):
            raise ToolError(f"{duration=} 必须是数字")
        if duration < 0:
            raise ToolError(f"{duration=} 必须是非负数")
        if duration > 100:
            raise ToolError(f"{duration=} 时间过长")
        await asyncio.sleep(duration)
        return await self.screenshot()

    # ==================== 内部辅助方法 ====================

    def validate_and_get_coordinates(self, coordinate: tuple[int, int] | None = None):
        """验证坐标参数并转换为屏幕坐标。

        参数:
            coordinate: API 坐标 [x, y]

        返回:
            转换后的屏幕坐标元组 (x, y)

        抛出:
            ToolError: 如果坐标格式无效
        """
        if not isinstance(coordinate, (list, tuple)) or len(coordinate) != 2:
            raise ToolError(f"{coordinate} 必须是长度为 2 的坐标")
        if not all(isinstance(i, int) and i >= 0 for i in coordinate):
            raise ToolError(f"{coordinate} 必须是非负整数坐标")

        return self.scale_coordinates(ScalingSource.API, coordinate[0], coordinate[1])

    async def shell(
        self,
        command: str,
        take_screenshot: bool = True,
        screenshot_delay: float | None = None,
    ) -> ToolResult:
        """执行 shell 命令并可选地截图。

        参数:
            command: 要执行的 shell 命令
            take_screenshot: 是否在命令执行后截图
            screenshot_delay: 截图前等待秒数（可选），None 则使用实例默认值

        返回:
            ToolResult: 包含命令输出、错误信息和可选截图的结果
        """
        _, stdout, stderr = await run(command)
        base64_image = None

        if take_screenshot:
            # 等待一段时间让界面稳定后再截图
            delay = screenshot_delay if screenshot_delay is not None else self._screenshot_delay
            await asyncio.sleep(delay)
            base64_image = (await self.screenshot()).base64_image

        return ToolResult(output=stdout, error=stderr, base64_image=base64_image)

    def scale_coordinates(self, source: ScalingSource, x: int, y: int):
        """在 API 坐标和屏幕坐标之间进行缩放转换。

        参数:
            source: 坐标来源（COMPUTER 表示从屏幕缩小，API 表示从 API 放大）
            x: X 坐标
            y: Y 坐标

        返回:
            转换后的坐标元组 (x, y)

        抛出:
            ToolError: 如果 API 坐标超出屏幕范围
        """
        if not self._scaling_enabled:
            return x, y

        # 根据宽高比找到匹配的目标分辨率
        ratio = self.width / self.height
        target_dimension = None
        for dimension in MAX_SCALING_TARGETS.values():
            # 允许宽高比有小误差
            if abs(dimension["width"] / dimension["height"] - ratio) < 0.02:
                if dimension["width"] < self.width:
                    target_dimension = dimension
                break

        if target_dimension is None:
            return x, y

        # 计算缩放因子（应小于 1）
        x_scaling_factor = target_dimension["width"] / self.width
        y_scaling_factor = target_dimension["height"] / self.height

        if source == ScalingSource.API:
            # 从 API 坐标放大到屏幕坐标
            if x > self.width or y > self.height:
                raise ToolError(f"坐标 {x}, {y} 超出屏幕范围")
            return round(x / x_scaling_factor), round(y / y_scaling_factor)

        # 从屏幕坐标缩小到 API 坐标
        return round(x * x_scaling_factor), round(y * y_scaling_factor)
