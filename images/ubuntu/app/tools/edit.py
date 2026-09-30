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
from dataclasses import dataclass
from pathlib import Path

from .base import CLIResult, ToolError, ToolResult, filesystem_error_detail
from .matcher import STRATEGY_LABELS, MatchError, apply_spans, find_replacement_spans
from .run import maybe_truncate, run

__all__ = ["EditTool", "MatchError"]

# 编辑片段显示的上下文行数
SNIPPET_LINES: int = 4


@dataclass(kw_only=True, frozen=True)
class FileViewResult(CLIResult):
    path: str
    next_offset: int | None = None
    next_column: int = 0
    truncated: bool = False
    total_lines: int | None = None


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

    async def view(
        self, path: str, view_range: list[int] | None = None, *,
        max_chars: int = 16000, column: int = 0,
    ) -> ToolResult:
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
        return await self._view(_path, view_range, max_chars=max_chars, column=column)

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

    async def str_replace(
        self,
        path: str,
        old_str: str,
        new_str: str | None = None,
        replace_all: bool = False,
        context: str | None = None,
    ) -> ToolResult:
        """在文件中进行字符串替换。

        参数:
            path: 文件的绝对路径
            old_str: 要替换的原始字符串
            new_str: 替换后的新字符串（None 表示删除）
            replace_all: 是否替换全部匹配（默认仅替换唯一匹配）
            context: 可选定位提示（如函数/类名行）；old_str 命中多处时
                优先取 context 之后的第一处

        返回:
            CLIResult: 包含替换结果和编辑片段的结果

        抛出:
            ToolError: 如果路径无效
            MatchError: 如果字符串未找到、不唯一或匹配跨度异常
        """
        _path = Path(path)
        self._validate_path("str_replace", _path)
        if old_str is None:
            raise ToolError("字符串替换操作必须提供 old_str 参数")
        return self._str_replace(_path, old_str, new_str, replace_all, context)

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

    async def _view(
        self, path: Path, view_range: list[int] | None = None, *,
        max_chars: int = 16000, column: int = 0,
    ):
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

        start, end = 1, -1
        if view_range is not None:
            if len(view_range) != 2 or not all(type(i) is int for i in view_range):
                raise ToolError("view_range 无效，必须是包含两个整数的列表。")
            start, end = view_range
        if start < 1 or (end != -1 and end < start) or column < 0 or max_chars < 1:
            raise ToolError("读取范围无效：起始行至少为 1，列偏移非负，结束行为 -1 或不小于起始行。")
        return await asyncio.to_thread(self._read_page, path, start, end, column, max_chars)

    def _read_page(self, path: Path, start: int, end: int, column: int, budget: int) -> FileViewResult:
        # 保持原有 UTF-8 → GB18030 → 替换回退；读取和跳行都以有界片段进行，
        # 不为文件总行数扫描剩余内容。total_lines 仅到 EOF 后可知。
        for encoding, errors in (("utf-8", "strict"), ("gb18030", "strict"), ("utf-8", "replace")):
            try:
                with path.open("r", encoding=encoding, errors=errors) as stream:
                    return self._read_page_stream(stream, path, start, end, column, budget)
            except UnicodeDecodeError:
                continue
            except OSError as exc:
                raise ToolError(f"读取 {path} 时遇到错误: {exc}") from exc
        raise AssertionError("replacement decoder cannot fail")

    @staticmethod
    def _read_page_stream(stream, path: Path, start: int, end: int, column: int, budget: int) -> FileViewResult:
        line, col = 1, 0
        fragments: list[str] = []
        rendered_line: int | None = None
        next_offset: int | None = None
        next_column = 0
        total_lines: int | None = None
        remaining = budget
        while True:
            # readline(size) 同时限制超长单行与普通文本；不缓存跳过的行。
            chunk = stream.readline(min(8192, max(1, remaining + 1)))
            if not chunk:
                total_lines = line if col else line - 1
                if not fragments and (start > max(1, total_lines) or column > col):
                    raise ToolError(f"读取起点超出文件末尾（共 {total_lines} 行）。")
                break
            ends_line = chunk.endswith("\n")
            chunk_col = col
            col += len(chunk)
            if line < start or (line == start and col <= column):
                if ends_line:
                    if line == start and column >= col:
                        raise ToolError("column 超出该行长度，请使用下一行 offset 并将 column 置 0。")
                    line, col = line + 1, 0
                continue
            if end != -1 and line > end:
                next_offset, next_column = line, chunk_col
                break
            if line == start and column > chunk_col:
                chunk = chunk[column - chunk_col:]
                chunk_col = column
            # 行号/列说明也计入预算；预留一个字符保证每页都有进展。
            prefix = "" if rendered_line == line else f"{line:6}\t"
            if rendered_line != line and chunk_col:
                prefix += f"[column={chunk_col}] "
            if remaining <= len(prefix) and fragments:
                next_offset, next_column = line, chunk_col
                break
            available = max(1, remaining - len(prefix))
            shown = chunk[:available]
            fragments.append(prefix + shown)
            rendered_line = line
            remaining -= len(prefix) + len(shown)
            if len(shown) < len(chunk):
                next_offset, next_column = line, chunk_col + len(shown)
                break
            if ends_line:
                line, col = line + 1, 0
            if remaining <= 0:
                if stream.read(1):
                    next_offset, next_column = line, col
                else:
                    total_lines = line if col else line - 1
                break
        content = "".join(fragments)
        output = f"以下是 {path} 的内容（带行号）:\n{content}"
        if next_offset is not None:
            output += f"\n[部分内容；继续读取 {path}：offset={next_offset}, column={next_column}]"
        else:
            output += f"\n[已到文件末尾，共 {total_lines} 行]"
        return FileViewResult(
            output=output, path=str(path), next_offset=next_offset, next_column=next_column,
            truncated=next_offset is not None, total_lines=total_lines,
        )

    def _str_replace(
        self,
        path: Path,
        old_str: str,
        new_str: str | None,
        replace_all: bool = False,
        context: str | None = None,
    ):
        """字符串替换的内部实现。

        原文保真：不做任何全局 expandtabs——文件中与本次编辑无关的 tab
        （Makefile 配方行、Go 缩进、TSV 等）绝不被改写；old_str 与文件间的
        tab/空格差异由匹配链的 tab_normalized 策略在「仅比较」层面容错。
        """
        file_content = self._read_file(path)
        new_str = new_str if new_str is not None else ""

        # 通过多级容错匹配链定位替换区间（未找到/不唯一/跨度异常时抛 MatchError）
        spans, strategy = find_replacement_spans(file_content, old_str, replace_all, context)

        # 执行替换
        new_file_content = apply_spans(file_content, spans, new_str)

        # 写入新内容
        self._write_file(path, new_file_content)

        # 保存历史记录
        self._file_history[path].append(file_content)

        # 创建首个编辑区域的代码片段
        replacement_line = file_content[: spans[0][0]].count("\n")
        start_line = max(0, replacement_line - SNIPPET_LINES)
        end_line = replacement_line + SNIPPET_LINES + new_str.count("\n")
        snippet = "\n".join(new_file_content.split("\n")[start_line: end_line + 1])

        # 构建成功消息
        success_msg = f"文件 {path} 已编辑（替换 {len(spans)} 处"
        if strategy != "exact":
            success_msg += f"，容错策略：{STRATEGY_LABELS[strategy]}"
        success_msg += "）。"
        success_msg += self._make_output(snippet, f"{path} 的片段", start_line + 1)
        success_msg += "请检查更改是否符合预期，必要时可再次编辑。"

        return CLIResult(output=success_msg)

    def _insert(self, path: Path, insert_line: int, new_str: str):
        """行插入的内部实现。原文保真：不展开制表符，插入文本按原样写入。"""
        file_text = self._read_file(path)
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

    # 编码探测顺序：UTF-8 严格解码优先；失败则尝试 GB18030（GBK/GB2312 超集，
    # 覆盖国内用户上传的中文 Office/SQL/CSV 等文本常见编码）；最后以 UTF-8 + 替换
    # 兜底，保证 view/replace 不因编码问题整体失败。
    _FALLBACK_ENCODINGS: tuple[str, ...] = ("utf-8", "gb18030")

    def _read_file(self, path: Path) -> str:
        """从指定路径读取文件内容。

        参数:
            path: 文件路径

        返回:
            文件内容字符串

        抛出:
            ToolError: 如果读取失败（非编码原因，如无权限）
        """
        try:
            raw = path.read_bytes()
        except Exception as e:
            raise ToolError(f"读取 {path} 时遇到错误: {e}") from None

        for encoding in self._FALLBACK_ENCODINGS:
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

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
        except OSError as exc:
            raise ToolError(filesystem_error_detail(path, exc)) from exc
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
