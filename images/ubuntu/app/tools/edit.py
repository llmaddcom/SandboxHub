"""
文件编辑工具模块 - 提供文件的查看、创建和编辑功能。

本模块实现了一个文件编辑器工具，支持：
- 查看文件/目录内容
- 创建新文件
- 字符串替换编辑
- 在指定行插入内容
"""

import asyncio
from collections import defaultdict
from pathlib import Path

from .base import CLIResult, ToolError, ToolResult
from .run import maybe_truncate, run

# 编辑片段显示的上下文行数
SNIPPET_LINES: int = 4


class EditTool:
    """文件编辑工具类。

    提供对文件系统的操作能力，包括查看、创建和编辑文件。
    维护文件编辑历史记录。

    属性:
        _file_history: 文件编辑历史字典，键为文件路径，值为历史内容列表
    """

    _file_history: dict[Path, list[str]]

    def __init__(self):
        """初始化文件编辑工具，创建空的历史记录。"""
        self._file_history = defaultdict(list)

    async def view(self, path: str, view_range: list[int] | None = None) -> ToolResult:
        """查看文件或目录内容。

        参数:
            path: 文件或目录的绝对路径
            view_range: 查看的行范围 [起始行, 结束行]（可选，仅对文件有效）

        返回:
            CLIResult: 包含文件/目录内容的结果

        抛出:
            ToolError: 如果路径无效或参数错误
        """
        _path = Path(path)
        self._validate_path("view", _path)
        return await self._view(_path, view_range)

    async def write(self, path: str, file_text: str) -> ToolResult:
        """创建或覆盖文件（upsert 语义）。

        参数:
            path: 文件的绝对路径
            file_text: 文件内容

        返回:
            ToolResult: 包含写入成功信息的结果

        抛出:
            ToolError: 如果路径不是绝对路径或写入失败
        """
        _path = Path(path)
        if not _path.is_absolute():
            raise ToolError(f"路径 {path} 不是绝对路径，应以 '/' 开头。")
        if file_text is None:
            raise ToolError("写入文件时必须提供 file_text 参数")
        # 自动创建父目录
        _path.parent.mkdir(parents=True, exist_ok=True)
        self._write_file(_path, file_text)
        self._file_history[_path].append(file_text)
        return ToolResult(output=f"文件已写入: {_path}")

    async def create(self, path: str, file_text: str) -> ToolResult:
        """创建新文件。

        参数:
            path: 新文件的绝对路径
            file_text: 文件内容

        返回:
            ToolResult: 包含创建成功信息的结果

        抛出:
            ToolError: 如果路径无效或文件已存在
        """
        _path = Path(path)
        self._validate_path("create", _path)
        if file_text is None:
            raise ToolError("创建文件时必须提供 file_text 参数")
        self._write_file(_path, file_text)
        self._file_history[_path].append(file_text)
        return ToolResult(output=f"文件创建成功: {_path}")

    async def str_replace(self, path: str, old_str: str, new_str: str | None = None) -> ToolResult:
        """在文件中进行字符串替换。

        参数:
            path: 文件的绝对路径
            old_str: 要替换的原始字符串
            new_str: 替换后的新字符串（None 表示删除）

        返回:
            CLIResult: 包含替换结果和编辑片段的结果

        抛出:
            ToolError: 如果路径无效、字符串未找到或存在多个匹配
        """
        _path = Path(path)
        self._validate_path("str_replace", _path)
        if old_str is None:
            raise ToolError("字符串替换操作必须提供 old_str 参数")
        return self._str_replace(_path, old_str, new_str)

    async def insert(self, path: str, insert_line: int, insert_text: str) -> ToolResult:
        """在文件指定行插入内容。

        参数:
            path: 文件的绝对路径
            insert_line: 插入位置的行号
            insert_text: 要插入的文本

        返回:
            CLIResult: 包含插入结果和编辑片段的结果

        抛出:
            ToolError: 如果路径无效或参数错误
        """
        _path = Path(path)
        self._validate_path("insert", _path)
        if insert_line is None:
            raise ToolError("插入操作必须提供 insert_line 参数")
        if insert_text is None:
            raise ToolError("插入操作必须提供 insert_text 参数")
        return self._insert(_path, insert_line, insert_text)

    # ==================== 内部实现方法 ====================

    def _validate_path(self, command: str, path: Path):
        """验证路径和命令的组合是否有效。

        参数:
            command: 操作命令名称
            path: 目标路径

        抛出:
            ToolError: 如果路径或命令无效
        """
        # 检查是否为绝对路径
        if not path.is_absolute():
            suggested_path = Path("") / path
            raise ToolError(
                f"路径 {path} 不是绝对路径，应以 '/' 开头。也许你想使用 {suggested_path}？"
            )
        # 检查路径是否存在（create 命令除外）
        if not path.exists() and command != "create":
            raise ToolError(
                f"路径 {path} 不存在，请提供有效路径。"
            )
        if path.exists() and command == "create":
            raise ToolError(
                f"文件已存在: {path}。无法使用 create 命令覆盖文件。"
            )
        # 检查是否为目录
        if path.is_dir():
            if command != "view":
                raise ToolError(
                    f"路径 {path} 是目录，只能使用 view 命令查看目录"
                )

    async def _view(self, path: Path, view_range: list[int] | None = None):
        """查看文件或目录内容的内部实现。"""
        if await asyncio.to_thread(path.is_dir):
            if view_range:
                raise ToolError("查看目录时不能使用 view_range 参数。")

            _, stdout, stderr = await run(
                rf"find {path} -maxdepth 2 -not -path '*/\.*'"
            )
            if not stderr:
                stdout = f"以下是 {path} 中深度不超过 2 层的文件和目录（不含隐藏项）:\n{stdout}\n"
            return CLIResult(output=stdout, error=stderr)

        file_content = self._read_file(path)
        init_line = 1
        if view_range:
            if len(view_range) != 2 or not all(isinstance(i, int) for i in view_range):
                raise ToolError("view_range 无效，必须是包含两个整数的列表。")
            file_lines = file_content.split("\n")
            n_lines_file = len(file_lines)
            init_line, final_line = view_range
            if init_line < 1 or init_line > n_lines_file:
                raise ToolError(
                    f"view_range 无效: {view_range}。起始行 {init_line} 应在文件行数范围 [1, {n_lines_file}] 内"
                )
            if final_line > n_lines_file:
                raise ToolError(
                    f"view_range 无效: {view_range}。结束行 {final_line} 应不超过文件总行数 {n_lines_file}"
                )
            if final_line != -1 and final_line < init_line:
                raise ToolError(
                    f"view_range 无效: {view_range}。结束行 {final_line} 应大于等于起始行 {init_line}"
                )

            if final_line == -1:
                file_content = "\n".join(file_lines[init_line - 1:])
            else:
                file_content = "\n".join(file_lines[init_line - 1: final_line])

        return CLIResult(
            output=self._make_output(file_content, str(path), init_line=init_line)
        )

    def _str_replace(self, path: Path, old_str: str, new_str: str | None):
        """字符串替换的内部实现。"""
        # 读取文件内容并规范化制表符
        file_content = self._read_file(path).expandtabs()
        old_str = old_str.expandtabs()
        new_str = new_str.expandtabs() if new_str is not None else ""

        # 检查 old_str 在文件中是否唯一
        occurrences = file_content.count(old_str)
        if occurrences == 0:
            raise ToolError(
                f"未执行替换，old_str `{old_str}` 在 {path} 中不存在。"
            )
        elif occurrences > 1:
            file_content_lines = file_content.split("\n")
            lines = [
                idx + 1
                for idx, line in enumerate(file_content_lines)
                if old_str in line
            ]
            raise ToolError(
                f"未执行替换。old_str `{old_str}` 在第 {lines} 行存在多个匹配，请确保它是唯一的"
            )

        # 执行替换
        new_file_content = file_content.replace(old_str, new_str)

        # 写入新内容
        self._write_file(path, new_file_content)

        # 保存历史记录
        self._file_history[path].append(file_content)

        # 创建编辑区域的代码片段
        replacement_line = file_content.split(old_str)[0].count("\n")
        start_line = max(0, replacement_line - SNIPPET_LINES)
        end_line = replacement_line + SNIPPET_LINES + new_str.count("\n")
        snippet = "\n".join(new_file_content.split("\n")[start_line: end_line + 1])

        # 构建成功消息
        success_msg = f"文件 {path} 已编辑。"
        success_msg += self._make_output(snippet, f"{path} 的片段", start_line + 1)
        success_msg += "请检查更改是否符合预期，必要时可再次编辑。"

        return CLIResult(output=success_msg)

    def _insert(self, path: Path, insert_line: int, new_str: str):
        """行插入的内部实现。"""
        file_text = self._read_file(path).expandtabs()
        new_str = new_str.expandtabs()
        file_text_lines = file_text.split("\n")
        n_lines_file = len(file_text_lines)

        if insert_line < 0 or insert_line > n_lines_file:
            raise ToolError(
                f"insert_line 参数无效: {insert_line}，应在文件行数范围 [0, {n_lines_file}] 内"
            )

        new_str_lines = new_str.split("\n")
        new_file_text_lines = (
            file_text_lines[:insert_line]
            + new_str_lines
            + file_text_lines[insert_line:]
        )
        snippet_lines = (
            file_text_lines[max(0, insert_line - SNIPPET_LINES): insert_line]
            + new_str_lines
            + file_text_lines[insert_line: insert_line + SNIPPET_LINES]
        )

        new_file_text = "\n".join(new_file_text_lines)
        snippet = "\n".join(snippet_lines)

        self._write_file(path, new_file_text)
        self._file_history[path].append(file_text)

        success_msg = f"文件 {path} 已编辑。"
        success_msg += self._make_output(
            snippet,
            "编辑后文件的片段",
            max(1, insert_line - SNIPPET_LINES + 1),
        )
        success_msg += "请检查更改是否符合预期（缩进正确、无重复行等），必要时可再次编辑。"
        return CLIResult(output=success_msg)

    def _read_file(self, path: Path) -> str:
        """从指定路径读取文件内容。

        参数:
            path: 文件路径

        返回:
            文件内容字符串

        抛出:
            ToolError: 如果读取失败
        """
        try:
            return path.read_text()
        except Exception as e:
            raise ToolError(f"读取 {path} 时遇到错误: {e}") from None

    def _write_file(self, path: Path, file: str):
        """将内容写入指定文件路径。

        参数:
            path: 文件路径
            file: 要写入的内容

        抛出:
            ToolError: 如果写入失败
        """
        try:
            path.write_text(file)
        except Exception as e:
            raise ToolError(f"写入 {path} 时遇到错误: {e}") from None

    def _make_output(
        self,
        file_content: str,
        file_descriptor: str,
        init_line: int = 1,
        expand_tabs: bool = True,
    ):
        """生成带行号的文件内容输出。

        参数:
            file_content: 文件内容
            file_descriptor: 文件描述信息
            init_line: 起始行号
            expand_tabs: 是否展开制表符

        返回:
            格式化后的输出字符串
        """
        file_content = maybe_truncate(file_content)
        if expand_tabs:
            file_content = file_content.expandtabs()
        file_content = "\n".join(
            [
                f"{i + init_line:6}\t{line}"
                for i, line in enumerate(file_content.split("\n"))
            ]
        )
        return (
            f"以下是 {file_descriptor} 的内容（带行号）:\n"
            + file_content
            + "\n"
        )
