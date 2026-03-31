import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from app.tools.bash import BashSession, OUTPUT_MAX_BYTES


def _make_stream_session(sentinel: str, stdout_lines: list[bytes], stderr: bytes = b""):
    """Return a started BashSession with mocked process for run_stream tests."""
    session = BashSession.__new__(BashSession)
    session._started = True
    session._timeout = 30.0
    session._sentinel = sentinel
    session._ec_prefix = f"__EC{sentinel}__"
    session._lock = asyncio.Lock()

    mock_stdout = AsyncMock()
    # readline() returns each line in order, then b"" to signal EOF
    mock_stdout.readline = AsyncMock(side_effect=stdout_lines + [b""])

    mock_stderr = AsyncMock()
    mock_stderr.read = AsyncMock(return_value=stderr)

    mock_process = MagicMock()
    mock_process.returncode = None
    mock_process.stdin = AsyncMock()
    mock_process.stdin.drain = AsyncMock()
    mock_process.stdout = mock_stdout
    mock_process.stderr = mock_stderr

    session._process = mock_process
    return session


@pytest.mark.asyncio
async def test_run_stream_yields_stdout_lines():
    sentinel = "SENT_STREAM_A"
    lines = [
        b"hello\n",
        b"world\n",
        f"__EC{sentinel}__0\n".encode(),
        f"{sentinel}\n".encode(),
    ]
    session = _make_stream_session(sentinel, lines)

    events = []
    async for event in session.run_stream("echo hello && echo world"):
        events.append(event)

    stdout_events = [e for e in events if e["type"] == "stdout"]
    assert len(stdout_events) == 2
    assert stdout_events[0]["chunk"] == "hello\n"
    assert stdout_events[1]["chunk"] == "world\n"


@pytest.mark.asyncio
async def test_run_stream_skips_ec_prefix_line():
    sentinel = "SENT_STREAM_B"
    lines = [
        b"output\n",
        f"__EC{sentinel}__0\n".encode(),
        f"{sentinel}\n".encode(),
    ]
    session = _make_stream_session(sentinel, lines)

    events = []
    async for event in session.run_stream("echo output"):
        events.append(event)

    all_chunks = " ".join(e.get("chunk", "") for e in events)
    assert "__EC" not in all_chunks
    assert sentinel not in all_chunks


@pytest.mark.asyncio
async def test_run_stream_ends_with_done_event():
    sentinel = "SENT_STREAM_C"
    lines = [
        b"line\n",
        f"__EC{sentinel}__0\n".encode(),
        f"{sentinel}\n".encode(),
    ]
    session = _make_stream_session(sentinel, lines)

    events = []
    async for event in session.run_stream("echo line"):
        events.append(event)

    assert events[-1] == {"type": "done"}


@pytest.mark.asyncio
async def test_run_stream_yields_stderr_after_completion():
    sentinel = "SENT_STREAM_D"
    lines = [
        f"__EC{sentinel}__1\n".encode(),
        f"{sentinel}\n".encode(),
    ]
    session = _make_stream_session(sentinel, lines, stderr=b"error message")

    events = []
    async for event in session.run_stream("bad_command"):
        events.append(event)

    stderr_events = [e for e in events if e["type"] == "stderr"]
    assert any("error message" in e["chunk"] for e in stderr_events)


@pytest.mark.asyncio
async def test_bash_tool_execute_stream_delegates_to_session():
    from app.tools.bash import BashTool
    tool = BashTool.__new__(BashTool)
    sentinel = "SENT_TOOL_A"
    lines = [
        b"result\n",
        f"__EC{sentinel}__0\n".encode(),
        f"{sentinel}\n".encode(),
    ]
    tool._session = _make_stream_session(sentinel, lines)

    events = []
    async for event in tool.execute_stream("echo result"):
        events.append(event)

    assert any(e["type"] == "stdout" and "result" in e["chunk"] for e in events)
    assert events[-1] == {"type": "done"}


@pytest.mark.asyncio
async def test_bash_tool_execute_stream_creates_session_if_none():
    from app.tools.bash import BashTool, BashSession
    tool = BashTool()
    assert tool._session is None

    # Patch BashSession.start so we don't spawn a real process
    original_start = BashSession.start

    async def fake_start(self):
        self._started = True
        sentinel = "FAKE_SENTINEL"
        self._sentinel = sentinel
        self._ec_prefix = f"__EC{sentinel}__"
        self._lock = asyncio.Lock()
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.stdin = AsyncMock()
        mock_proc.stdin.drain = AsyncMock()
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(side_effect=[
            b"hi\n",
            f"__EC{sentinel}__0\n".encode(),
            f"{sentinel}\n".encode(),
            b"",
        ])
        mock_stderr = AsyncMock()
        mock_stderr.read = AsyncMock(return_value=b"")
        mock_proc.stdout = mock_stdout
        mock_proc.stderr = mock_stderr
        self._process = mock_proc

    BashSession.start = fake_start
    try:
        events = []
        async for event in tool.execute_stream("echo hi"):
            events.append(event)
        assert tool._session is not None
        assert any(e.get("chunk", "").strip() == "hi" for e in events)
    finally:
        BashSession.start = original_start
