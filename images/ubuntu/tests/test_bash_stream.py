"""旧契约 SSE（BashTool.execute_stream）——基于 job 日志按行推送。"""

import pytest

from app.tools.bash import BashTool


async def _collect(tool: BashTool, command: str, timeout: float | None = None) -> list[dict]:
    return [e async for e in tool.execute_stream(command, timeout)]


@pytest.mark.asyncio
async def test_run_stream_yields_stdout_lines_in_order():
    events = await _collect(BashTool(), "echo hello; sleep 0.2; echo world")
    stdout = [e["chunk"] for e in events if e["type"] == "stdout"]
    assert stdout == ["hello\n", "world\n"]


@pytest.mark.asyncio
async def test_run_stream_flushes_trailing_partial_line():
    events = await _collect(BashTool(), "printf 'a\\nb'")
    stdout = [e["chunk"] for e in events if e["type"] == "stdout"]
    assert stdout == ["a\n", "b"]


@pytest.mark.asyncio
async def test_run_stream_ends_with_done_event():
    events = await _collect(BashTool(), "echo line")
    assert events[-1] == {"type": "done"}


@pytest.mark.asyncio
async def test_run_stream_merges_stderr_into_stdout_stream():
    events = await _collect(BashTool(), "echo bad >&2; exit 1")
    assert any(e["type"] == "stdout" and "bad" in e["chunk"] for e in events)


@pytest.mark.asyncio
async def test_run_stream_reports_timeout_as_stderr_event():
    events = await _collect(BashTool(), "sleep 30", timeout=0.3)
    stderr = [e["chunk"] for e in events if e["type"] == "stderr"]
    assert any("超时" in c for c in stderr)
    assert events[-1] == {"type": "done"}


@pytest.mark.asyncio
async def test_stream_preserves_cwd_for_following_calls(tmp_path):
    tool = BashTool()
    await _collect(tool, f"cd {tmp_path}")
    events = await _collect(tool, "pwd")
    assert events[0]["chunk"].strip() == str(tmp_path.resolve())
