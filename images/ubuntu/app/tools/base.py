"""
基础工具模块 - 定义工具执行结果和异常的基础数据类。

本模块提供了所有工具操作的基础数据结构，包括：
- ToolResult: 工具执行结果的数据类
- CLIResult: 命令行输出结果
- ToolFailure: 工具执行失败的结果
- ToolError: 工具执行异常
"""

from dataclasses import dataclass, fields, replace
import errno
import os


def _env_roots(name: str) -> list[str]:
    """Hub 按目录布局注入的路径清单（冒号分隔）；未注入时为空。"""
    return [root for root in os.getenv(name, "").split(":") if root.startswith("/")]


def _under(path, roots: list[str]) -> str | None:
    value = os.path.normpath(os.fspath(path))
    for root in roots:
        if value == root or value.startswith(root.rstrip("/") + "/"):
            return root
    return None


def hidden_root_error(path) -> str | None:
    """写入平台保留目录（如布局 v2 的 /self/.cr）时的拒绝原因；不在其中返回 None。

    这些目录在实例里列不出、写入也不会进存储，静默成功只会让写入方以为数据已保存。
    """
    root = _under(path, _env_roots("SANDBOX_HIDDEN_ROOTS"))
    if root is None:
        return None
    return f"{path} 在平台保留目录 {root} 下，不能写入；请写到工作目录或自己的目录里。"


def filesystem_error_detail(path, exc: OSError) -> str:
    """区分只读位置、物理磁盘/配额耗尽与应用层文件大小限制。"""
    if exc.errno == errno.EROFS:
        roots = _env_roots("SANDBOX_READONLY_ROOTS")
        listed = f"只读位置：{'、'.join(roots)}；" if roots else ""
        cwd = os.getenv("SANDBOX_CWD", "")
        copy = f"要修改先复制到 {cwd} 下再改" if cwd else "要修改先复制到可写目录再改"
        return f"{path} 在只读位置，不能写入（{listed}{copy}）。"
    if exc.errno not in (errno.ENOSPC, errno.EDQUOT):
        return f"{path}: {exc}"
    parent = os.path.dirname(str(path)) or "/"
    try:
        stat = os.statvfs(parent)
        space = f"，挂载点可用 {stat.f_bavail * stat.f_frsize} 字节"
    except OSError:
        space = ""
    return (
        f"存储空间或文件系统配额不足：{path}{space}（errno={exc.errno}）。"
        "请检查该路径所在磁盘、Docker 数据盘及 /tmp/rclone-mount.log；"
        "这是底层存储限制，增大上传大小参数不会恢复空间。"
    )


@dataclass(kw_only=True, frozen=True)
class ToolResult:
    """工具执行结果数据类。

    属性:
        output: 工具执行的标准输出（可选）
        error: 工具执行的错误输出（可选）
        base64_image: Base64 编码的截图图片（可选）
        system: 系统级别的提示信息（可选）
    """

    output: str | None = None
    error: str | None = None
    base64_image: str | None = None
    system: str | None = None

    def __bool__(self):
        """判断结果是否包含任何有效数据。"""
        return any(getattr(self, field.name) for field in fields(self))

    def __add__(self, other: "ToolResult"):
        """合并两个工具执行结果。"""

        def combine_fields(
            field: str | None, other_field: str | None, concatenate: bool = True
        ):
            """合并两个字段的值。

            参数:
                field: 第一个字段值
                other_field: 第二个字段值
                concatenate: 是否允许拼接（对于图片字段应为 False）
            """
            if field and other_field:
                if concatenate:
                    return field + other_field
                raise ValueError("无法合并工具结果")
            return field or other_field

        return ToolResult(
            output=combine_fields(self.output, other.output),
            error=combine_fields(self.error, other.error),
            base64_image=combine_fields(self.base64_image, other.base64_image, False),
            system=combine_fields(self.system, other.system),
        )

    def replace(self, **kwargs):
        """返回一个替换了指定字段的新 ToolResult 实例。"""
        return replace(self, **kwargs)


class CLIResult(ToolResult):
    """命令行输出结果 - 可以作为 CLI 输出进行渲染。"""


class ToolFailure(ToolResult):
    """工具执行失败的结果。"""


class ToolError(Exception):
    """工具执行异常 - 当工具遇到错误时抛出。

    属性:
        message: 错误信息描述
    """

    def __init__(self, message):
        self.message = message
