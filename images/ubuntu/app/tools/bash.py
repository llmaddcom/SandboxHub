"""
终端工具模块 - 提供 Bash 终端会话管理和命令执行功能。

本模块实现了一个异步的 Bash 终端会话，支持：
- 启动和停止终端会话
- 在终端中执行命令并获取输出
- 命令执行超时控制（可配置，默认 30s，最大 300s）
- 输出截断（50KB 上限，防止大输出撑爆内存）
- 崩溃自动重建（session 进程退出或超时后自动恢复）
- 会话重启功能
"""

import asyncio
import os
import shlex
import uuid

from .base import CLIResult, ToolError, ToolResult

DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 300.0
OUTPUT_MAX_BYTES = 50 * 1024  # 50KB

TRUNCATION_NOTICE = "\n[...输出已在 50KB 处截断...]"

# StreamReader 缓冲区上限（需大于 OUTPUT_MAX_BYTES，防止 LimitOverrunError）
_STREAM_LIMIT = 256 * 1024  # 256KB

HEAD_BYTES = 25 * 1024   # 25 KB
TAIL_BYTES = 25 * 1024   # 25 KB


def _head_tail_truncate(text: str) -> str:
    """Keep first HEAD_BYTES + last TAIL_BYTES; replace middle with notice."""
    b = text.encode()
    if len(b) <= HEAD_BYTES + TAIL_BYTES:
        return text
    omitted = len(b) - HEAD_BYTES - TAIL_BYTES
    return (
        b[:HEAD_BYTES].decode(errors="replace")
        + f"\n[...{omitted} bytes 已省略...]\n"
        + b[-TAIL_BYTES:].decode(errors="replace")
    )


class BashSession:
    """Bash 终端会话管理类。

    管理一个持久化的 bash 进程，支持连续执行命令。
    使用哨兵字符串来检测命令执行完成。

    属性:
        command: 使用的 shell 命令（默认 /bin/bash）
        _sentinel: 用于检测命令完成的哨兵字符串（每个实例唯一 UUID）
        _lock: 并发保护锁，防止多请求同时操作同一 bash 进程
    """

    _started: bool
    _process: asyncio.subprocess.Process

    command: str = "/bin/bash"

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        self._started = False
        self._timeout = min(timeout, MAX_TIMEOUT)
        # 每实例唯一哨兵，防止命令输出恰好包含哨兵字符串
        self._sentinel = f"__SENT_{uuid.uuid4().hex}__"
        self._ec_prefix = f"__EC{self._sentinel}__"
        # 并发保护锁
        self._lock = asyncio.Lock()

    async def start(self):
        """启动 bash 会话进程。

        如果会话已经启动，则跳过。
        创建一个新的子进程，配置 stdin/stdout/stderr 管道。
        """
        if self._started:
            return

        self._process = await asyncio.create_subprocess_shell(
            self.command,
            preexec_fn=os.setsid,
            shell=True,
            bufsize=0,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT,
        )

        self._started = True

    def stop(self):
        """终止 bash 会话进程。

        抛出:
            ToolError: 如果会话尚未启动
        """
        if not self._started:
            raise ToolError("会话尚未启动。")
        if self._process.returncode is not None:
            return
        self._process.terminate()

    async def run(self, command: str, timeout: float | None = None):
        """Execute command. Timeout kills only the inner subprocess; session survives."""
        if not self._started:
            raise ToolError("会话尚未启动。")
        if self._process.returncode is not None:
            return ToolResult(
                system="工具需要重启",
                error=f"bash 已退出，返回码为 {self._process.returncode}",
            )

        effective_timeout = self._timeout if timeout is None else min(timeout, MAX_TIMEOUT)

        assert self._process.stdin
        assert self._process.stdout
        assert self._process.stderr

        async with self._lock:
            # Wrap: inner timeout kills user command; outer bash always prints sentinel
            stdin_cmd = (
                f"timeout {effective_timeout}s bash -c {shlex.quote(command)}; "
                f"__ec_sb=$?; "
                f"printf '\\n{self._ec_prefix}%s\\n{self._sentinel}\\n' $__ec_sb\n"
            )
            self._process.stdin.write(stdin_cmd.encode())
            await self._process.stdin.drain()

            sentinel_bytes = f"{self._sentinel}\n".encode()

            try:
                raw = await self._process.stdout.readuntil(sentinel_bytes)
            except asyncio.LimitOverrunError:
                # Output exceeded StreamReader buffer — drain until sentinel, return notice
                sentinel_b = self._sentinel.encode()
                tail = b""
                while True:
                    chunk = await self._process.stdout.read(4096)
                    if not chunk:
                        break
                    combined = tail + chunk
                    if sentinel_b in combined:
                        break
                    tail = combined[-(len(sentinel_b) - 1):]  # keep overlap window
                return CLIResult(
                    output=f"[输出超过 {_STREAM_LIMIT // 1024}KB 缓冲区上限，已截断]{TRUNCATION_NOTICE}",
                    error="",
                )

            # Parse: strip sentinel line, then extract EC_PREFIX line
            decoded = raw.decode(errors="replace")
            sentinel_idx = decoded.rfind(self._sentinel)
            content = decoded[:sentinel_idx].rstrip("\n")

            ec_idx = content.rfind(self._ec_prefix)
            if ec_idx >= 0:
                ec_str = content[ec_idx + len(self._ec_prefix):].strip()
                try:
                    exit_code = int(ec_str)
                except ValueError:
                    exit_code = -1
                output = content[:ec_idx].rstrip("\n")
            else:
                exit_code = 0
                output = content

            # stderr — non-blocking read after command completes
            error = ""
            try:
                async with asyncio.timeout(0.1):
                    raw_err = await self._process.stderr.read(OUTPUT_MAX_BYTES + 1)
                    error = raw_err.decode(errors="replace").rstrip("\n")
            except asyncio.TimeoutError:
                pass

            # HeadTailBuffer truncation
            output = _head_tail_truncate(output)
            error = _head_tail_truncate(error)

            # Timeout notice (exit code 124 = timeout)
            if exit_code == 124:
                notice = f"[命令已超时 ({effective_timeout}s)，进程已终止，session 继续可用]"
                output = f"{notice}\n{output}".strip()

        return CLIResult(output=output, error=error)

    async def run_stream(self, command: str, timeout: float | None = None):
        """Yield stdout chunks line-by-line as command runs; yield done when complete.

        Yields dicts:
            {"type": "stdout", "chunk": str}  — each stdout line as it arrives
            {"type": "stderr", "chunk": str}  — full stderr after completion (if any)
            {"type": "done"}                  — command finished
        """
        if not self._started:
            raise ToolError("会话尚未启动。")
        if self._process.returncode is not None:
            yield {"type": "stderr", "chunk": f"bash 已退出，返回码为 {self._process.returncode}"}
            yield {"type": "done"}
            return

        effective_timeout = self._timeout if timeout is None else min(timeout, MAX_TIMEOUT)

        assert self._process.stdin
        assert self._process.stdout
        assert self._process.stderr

        async with self._lock:
            stdin_cmd = (
                f"timeout {effective_timeout}s bash -c {shlex.quote(command)}; "
                f"__ec_sb=$?; "
                f"printf '\\n{self._ec_prefix}%s\\n{self._sentinel}\\n' $__ec_sb\n"
            )
            self._process.stdin.write(stdin_cmd.encode())
            await self._process.stdin.drain()

            try:
                async with asyncio.timeout(effective_timeout + 10):
                    while True:
                        line_bytes = await self._process.stdout.readline()
                        if not line_bytes:
                            break
                        line = line_bytes.decode(errors="replace")
                        if line.rstrip("\n") == self._sentinel:
                            break
                        if line.startswith(self._ec_prefix):
                            continue
                        yield {"type": "stdout", "chunk": line}
            except asyncio.TimeoutError:
                yield {"type": "stderr", "chunk": f"[命令超时 ({effective_timeout}s)]"}

            # Non-blocking stderr drain after command completes
            try:
                async with asyncio.timeout(0.1):
                    raw_err = await self._process.stderr.read(OUTPUT_MAX_BYTES + 1)
                    error = raw_err.decode(errors="replace").rstrip("\n")
                    if error:
                        yield {"type": "stderr", "chunk": error}
            except asyncio.TimeoutError:
                pass

            yield {"type": "done"}


class BashTool:
    """Bash 终端工具类。

    封装 BashSession，提供命令执行和会话重启的高级接口。
    自动检测崩溃的 session 并重建。
    """

    _session: BashSession | None

    def __init__(self):
        self._session = None

    async def execute(self, command: str, timeout: float | None = None) -> ToolResult:
        """执行一条 bash 命令。

        如果会话尚未启动，会自动创建并启动。
        如果会话已崩溃或上次超时，会自动重建并在结果中告知 LLM。

        参数:
            command: 要执行的 bash 命令
            timeout: 命令超时秒数（可选），None 使用默认值

        返回:
            ToolResult: 包含命令输出和错误信息的结果

        抛出:
            ToolError: 如果未提供命令
        """
        if self._session is None:
            self._session = BashSession()
            await self._session.start()

        # 检测 session 崩溃，自动重建并警告 LLM
        if self._session._started and (
            self._session._process.returncode is not None
        ):
            self._session.stop()
            self._session = BashSession()
            await self._session.start()
            result = await self._session.run(command, timeout=timeout)
            return ToolResult(
                output=result.output,
                error=result.error,
                system="⚠️ bash session 已重建（进程崩溃）。工作目录已重置为 $HOME，环境变量已清空。",
            )

        return await self._session.run(command, timeout=timeout)

    async def execute_stream(self, command: str, timeout: float | None = None):
        """Stream command output; yields same event dicts as BashSession.run_stream().

        Auto-creates session on first call. Detects crashed session and rebuilds it,
        yielding a warning event before streaming the command.
        """
        if self._session is None:
            self._session = BashSession()
            await self._session.start()

        if self._session._started and self._session._process.returncode is not None:
            self._session.stop()
            self._session = BashSession()
            await self._session.start()
            yield {
                "type": "stderr",
                "chunk": "⚠️ bash session 已重建（进程崩溃）。工作目录已重置为 $HOME，环境变量已清空。",
            }

        async for event in self._session.run_stream(command, timeout=timeout):
            yield event

    async def restart(self) -> ToolResult:
        """重启终端会话。

        停止当前会话（如果存在），然后创建并启动新会话。

        返回:
            ToolResult: 包含重启成功信息的结果
        """
        if self._session:
            self._session.stop()
        self._session = BashSession()
        await self._session.start()

        return ToolResult(system="终端会话已重启。")
