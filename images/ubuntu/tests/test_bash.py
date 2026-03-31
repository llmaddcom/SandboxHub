import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.tools.bash import _head_tail_truncate, HEAD_BYTES, TAIL_BYTES, BashSession, BashTool
from src.tools.base import ToolResult


def test_head_tail_short_text_unchanged():
    text = "hello world"
    assert _head_tail_truncate(text) == text


def test_head_tail_exact_limit_unchanged():
    text = "x" * (HEAD_BYTES + TAIL_BYTES)
    assert _head_tail_truncate(text) == text


def test_head_tail_truncates_middle():
    # 1 byte over limit so truncation kicks in
    text = "A" * HEAD_BYTES + "M" * 10 + "Z" * TAIL_BYTES
    result = _head_tail_truncate(text)
    assert result.startswith("A" * HEAD_BYTES)
    assert result.endswith("Z" * TAIL_BYTES)
    assert "已省略" in result
    assert "M" not in result


def test_head_tail_reports_omitted_byte_count():
    extra = 100
    text = "x" * (HEAD_BYTES + TAIL_BYTES + extra)
    result = _head_tail_truncate(text)
    assert str(extra) in result


# ── BashSession helpers ──────────────────────────────────────────────────────

def _make_session_with_mock_process(sentinel: str, stdout_bytes: bytes):
    """Return a started BashSession with a mocked process."""
    session = BashSession.__new__(BashSession)
    session._started = True
    session._timeout = 30.0
    session._sentinel = sentinel
    session._ec_prefix = f"__EC{sentinel}__"
    session._lock = asyncio.Lock()

    mock_stdout = AsyncMock()
    mock_stdout.readuntil = AsyncMock(return_value=stdout_bytes)
    mock_stderr = AsyncMock()
    mock_stderr.read = AsyncMock(return_value=b"")

    mock_stdin = AsyncMock()
    mock_stdin.write = MagicMock()
    mock_stdin.drain = AsyncMock()

    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.stdout = mock_stdout
    mock_proc.stderr = mock_stderr
    mock_proc.stdin = mock_stdin

    session._process = mock_proc
    return session


@pytest.mark.asyncio
async def test_session_run_returns_output_on_success():
    sentinel = "__SENT_test__"
    ec_prefix = f"__EC{sentinel}__"
    stdout = f"hello\n{ec_prefix}0\n{sentinel}\n".encode()
    session = _make_session_with_mock_process(sentinel, stdout)

    result = await session.run("echo hello")

    assert result.output == "hello"
    assert result.error == ""


@pytest.mark.asyncio
async def test_session_run_timeout_returns_notice_not_exception():
    sentinel = "__SENT_test2__"
    ec_prefix = f"__EC{sentinel}__"
    stdout = f"partial\n{ec_prefix}124\n{sentinel}\n".encode()
    session = _make_session_with_mock_process(sentinel, stdout)

    result = await session.run("sleep 100")

    assert "超时" in result.output
    assert "30" in result.output
    assert "session 继续可用" in result.output
    assert session._process.returncode is None


@pytest.mark.asyncio
async def test_session_run_preserves_session_after_timeout():
    sentinel = "__SENT_test3__"
    ec_prefix = f"__EC{sentinel}__"
    stdout = f"\n{ec_prefix}124\n{sentinel}\n".encode()
    session = _make_session_with_mock_process(sentinel, stdout)

    await session.run("sleep 100")

    assert session._process.returncode is None


@pytest.mark.asyncio
async def test_session_run_applies_head_tail_to_large_output():
    sentinel = "__SENT_test4__"
    ec_prefix = f"__EC{sentinel}__"
    big = "x" * (HEAD_BYTES + TAIL_BYTES + 500)
    stdout = f"{big}\n{ec_prefix}0\n{sentinel}\n".encode()
    session = _make_session_with_mock_process(sentinel, stdout)

    result = await session.run("cat bigfile")

    assert "已省略" in result.output
    assert len(result.output.encode()) < len(big.encode())


@pytest.mark.asyncio
async def test_session_raises_on_not_started():
    session = BashSession()
    with pytest.raises(Exception, match="未启动|尚未启动"):
        await session.run("echo hi")


# ── BashTool tests ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bash_tool_does_not_rebuild_session_after_timeout():
    """After a timeout the session must NOT be replaced — cwd is preserved."""
    sentinel = "__SENT_bt1__"
    ec_prefix = f"__EC{sentinel}__"
    timeout_stdout = f"\n{ec_prefix}124\n{sentinel}\n".encode()

    tool = BashTool()
    tool._session = BashSession.__new__(BashSession)
    tool._session._started = True
    tool._session._timeout = 30.0
    tool._session._sentinel = sentinel
    tool._session._ec_prefix = ec_prefix
    tool._session._lock = asyncio.Lock()

    mock_stdout = AsyncMock()
    mock_stdout.readuntil = AsyncMock(return_value=timeout_stdout)
    mock_stderr = AsyncMock()
    mock_stderr.read = AsyncMock(return_value=b"")
    mock_stdin = AsyncMock()
    mock_stdin.write = MagicMock()
    mock_stdin.drain = AsyncMock()
    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.stdout = mock_stdout
    mock_proc.stderr = mock_stderr
    mock_proc.stdin = mock_stdin
    tool._session._process = mock_proc

    first_session_id = id(tool._session)
    await tool.execute("sleep 100")
    assert id(tool._session) == first_session_id, "Session was rebuilt — should not happen on timeout"


@pytest.mark.asyncio
async def test_bash_tool_rebuilds_session_on_process_exit():
    """If bash process dies (returncode set), BashTool rebuilds and warns."""
    tool = BashTool()

    dead_session = MagicMock()
    dead_session._started = True
    dead_session._process = MagicMock(returncode=1)
    dead_session.stop = MagicMock()
    tool._session = dead_session

    new_session = MagicMock()
    new_session.run = AsyncMock(return_value=ToolResult(output="ok"))
    new_session.start = AsyncMock()

    with patch("src.tools.bash.BashSession", return_value=new_session):
        result = await tool.execute("echo ok")

    assert "重建" in (result.system or "")
    assert "崩溃" in (result.system or "")
