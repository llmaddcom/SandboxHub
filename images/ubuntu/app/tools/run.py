"""
Shell 命令执行工具模块 - 提供异步执行 shell 命令的功能。

本模块提供了：
- run: 异步执行 shell 命令并返回结果
- maybe_truncate: 截断过长的输出内容
"""

import asyncio

# 截断提示信息
TRUNCATED_MESSAGE: str = "<响应已截断><提示>为节省上下文，仅显示部分内容。</提示>"

# 最大响应长度
MAX_RESPONSE_LEN: int = 16000


def maybe_truncate(content: str, truncate_after: int | None = MAX_RESPONSE_LEN):
    """截断过长的内容并附加提示。

    参数:
        content: 要处理的内容字符串
        truncate_after: 截断阈值（字符数），None 表示不截断

    返回:
        处理后的字符串，超长时会被截断并附加提示
    """
    return (
        content
        if not truncate_after or len(content) <= truncate_after
        else content[:truncate_after] + TRUNCATED_MESSAGE
    )


async def run(
    cmd: str,
    timeout: float | None = 120.0,  # 超时时间（秒）
    truncate_after: int | None = MAX_RESPONSE_LEN,
):
    """异步执行 shell 命令。

    参数:
        cmd: 要执行的 shell 命令字符串
        timeout: 命令超时时间（秒），None 表示不限时
        truncate_after: 输出截断阈值（字符数）

    返回:
        元组 (返回码, 标准输出, 标准错误)

    抛出:
        TimeoutError: 如果命令执行超时
    """
    process = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        return (
            process.returncode or 0,
            maybe_truncate(stdout.decode(), truncate_after=truncate_after),
            maybe_truncate(stderr.decode(), truncate_after=truncate_after),
        )
    except asyncio.TimeoutError as exc:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        raise TimeoutError(
            f"命令 '{cmd}' 在 {timeout} 秒后超时"
        ) from exc
