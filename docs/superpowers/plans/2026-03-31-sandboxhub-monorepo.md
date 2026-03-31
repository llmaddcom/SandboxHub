# SandboxHub Monorepo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge Ubuntu sandbox project into SandboxHub monorepo, add root entry point, fix Dockerfile network issues, and add SSE streaming to terminal execution.

**Architecture:** Ubuntu project files copy into `images/ubuntu/{scripts,app}/`; router imports switch from `computer_use_demo.X` to `..X` (relative); a root `main.py` with `sys.path` injection drives uvicorn; `BashSession.run_stream()` async generator streams stdout line-by-line; a new `POST /api/terminal/execute/stream` SSE endpoint exposes it.

**Tech Stack:** Python 3.11+, FastAPI, asyncio, pytest-asyncio, Gitee mirrors (noVNC/pyenv)

---

### Task 1: Create monorepo directory structure

**Files:**
- Create: `images/ubuntu/scripts/` (from Ubuntu `image/`)
- Create: `images/ubuntu/app/` (from Ubuntu `src/`)
- Create: `images/ubuntu/Dockerfile` (from Ubuntu `Dockerfile`)
- Create: `images/ubuntu/tests/` (from Ubuntu `tests/`)

- [ ] **Step 1: Create target directories and copy files**

```bash
cd /data/zh/SandboxHub
mkdir -p images/ubuntu

# Copy all three components
cp -r /data/zh/Ubuntu/image images/ubuntu/scripts
cp -r /data/zh/Ubuntu/src   images/ubuntu/app
cp    /data/zh/Ubuntu/Dockerfile images/ubuntu/Dockerfile
cp -r /data/zh/Ubuntu/tests images/ubuntu/tests
```

- [ ] **Step 2: Verify structure**

```bash
find images/ubuntu -maxdepth 3 -type f | sort
```

Expected output includes:
```
images/ubuntu/Dockerfile
images/ubuntu/app/__init__.py
images/ubuntu/app/main.py
images/ubuntu/app/mcp_server.py
images/ubuntu/app/routers/terminal.py
images/ubuntu/app/tools/bash.py
images/ubuntu/scripts/entrypoint.sh
images/ubuntu/scripts/start_all.sh
images/ubuntu/tests/test_bash.py
```

- [ ] **Step 3: Commit**

```bash
git add images/
git commit -m "feat: migrate Ubuntu sandbox into images/ubuntu/ monorepo structure"
```

---

### Task 2: Update Dockerfile COPY paths and build context

**Files:**
- Modify: `images/ubuntu/Dockerfile`

- [ ] **Step 1: Update COPY instructions**

In `images/ubuntu/Dockerfile`, find and replace these two COPY lines near the bottom:

Old:
```dockerfile
COPY --chown=$USERNAME:$USERNAME image/ $HOME
COPY --chown=$USERNAME:$USERNAME src/ $HOME/computer_use_demo/
```

New:
```dockerfile
COPY --chown=$USERNAME:$USERNAME scripts/ $HOME/
COPY --chown=$USERNAME:$USERNAME app/ $HOME/computer_use_demo/
```

- [ ] **Step 2: Verify the change**

```bash
grep -n "COPY" images/ubuntu/Dockerfile
```

Expected: lines showing `scripts/` and `app/` in the COPY commands.

- [ ] **Step 3: Test build command syntax (dry run)**

```bash
# Verify docker can parse the Dockerfile without errors
docker build --no-cache --dry-run images/ubuntu/ 2>&1 | head -20 || \
docker build -f images/ubuntu/Dockerfile images/ubuntu/ --progress=plain 2>&1 | head -5
```

If `--dry-run` is not available on this Docker version, just verify the file is syntactically valid:
```bash
docker build -f images/ubuntu/Dockerfile images/ubuntu/ --target nonexistent 2>&1 | head -3
```
Expected: error mentions `nonexistent` stage (not a syntax error).

- [ ] **Step 4: Commit**

```bash
git add images/ubuntu/Dockerfile
git commit -m "fix: update Dockerfile COPY paths for monorepo (scripts/, app/)"
```

---

### Task 3: Replace GitHub clones with Gitee mirrors in Dockerfile

**Files:**
- Modify: `images/ubuntu/Dockerfile`

- [ ] **Step 1: Replace noVNC GitHub clone**

Find in `images/ubuntu/Dockerfile`:
```dockerfile
RUN git clone --branch v1.5.0 https://github.com/novnc/noVNC.git /opt/noVNC && \
    git clone --branch v0.12.0 https://github.com/novnc/websockify /opt/noVNC/utils/websockify && \
    ln -s /opt/noVNC/vnc.html /opt/noVNC/index.html
```

Replace with:
```dockerfile
RUN git clone --branch v1.5.0 https://gitee.com/mirrors/noVNC.git /opt/noVNC && \
    git clone --branch v0.12.0 https://gitee.com/mirrors/websockify /opt/noVNC/utils/websockify && \
    ln -s /opt/noVNC/vnc.html /opt/noVNC/index.html
```

- [ ] **Step 2: Replace pyenv GitHub clone**

Find in `images/ubuntu/Dockerfile`:
```dockerfile
RUN git clone https://github.com/pyenv/pyenv.git ~/.pyenv && \
```

Replace with:
```dockerfile
RUN git clone https://gitee.com/mirrors/pyenv.git ~/.pyenv && \
```

- [ ] **Step 3: Verify no remaining github.com/novnc or github.com/pyenv references**

```bash
grep -n "github.com" images/ubuntu/Dockerfile
```

Expected: no output (all GitHub refs replaced).

- [ ] **Step 4: Commit**

```bash
git add images/ubuntu/Dockerfile
git commit -m "fix: replace GitHub clones with Gitee mirrors for reliable CN builds"
```

---

### Task 4: Update router imports from computer_use_demo to relative

**Files:**
- Modify: `images/ubuntu/app/routers/terminal.py`
- Modify: `images/ubuntu/app/routers/file.py`
- Modify: `images/ubuntu/app/routers/screen.py`
- Modify: `images/ubuntu/app/routers/mouse.py`
- Modify: `images/ubuntu/app/routers/keyboard.py`
- Modify: `images/ubuntu/app/routers/system.py`
- Modify: `images/ubuntu/app/routers/process.py`
- Modify: `images/ubuntu/app/routers/browser.py`
- Modify: `images/ubuntu/app/mcp_server.py`

Inside the container the app copies to `$HOME/computer_use_demo/` so `computer_use_demo.tools` worked at runtime. After moving to `images/ubuntu/app/`, we switch to relative imports so the code works both inside the container (run as `python -m computer_use_demo.main`) and locally (run as `python -m app.main` with `PYTHONPATH=images/ubuntu`).

- [ ] **Step 1: Replace imports in all router files**

```bash
cd /data/zh/SandboxHub

# Replace absolute package imports with relative ones in all router files
sed -i 's/from computer_use_demo\.tools import/from ..tools import/g' \
    images/ubuntu/app/routers/terminal.py \
    images/ubuntu/app/routers/file.py \
    images/ubuntu/app/routers/screen.py \
    images/ubuntu/app/routers/mouse.py \
    images/ubuntu/app/routers/keyboard.py \
    images/ubuntu/app/routers/system.py \
    images/ubuntu/app/routers/process.py \
    images/ubuntu/app/routers/browser.py

sed -i 's/from computer_use_demo\.tools\.run import/from ..tools.run import/g' \
    images/ubuntu/app/routers/system.py \
    images/ubuntu/app/routers/process.py \
    images/ubuntu/app/routers/browser.py
```

- [ ] **Step 2: Fix mcp_server.py (top-level module, uses single dot)**

`mcp_server.py` is at the root of `app/`, so its imports use a single dot (`.tools` not `..tools`):

```bash
sed -i 's/from computer_use_demo\./from ./g' images/ubuntu/app/mcp_server.py
```

Verify:
```bash
grep -n "computer_use_demo\|from \." images/ubuntu/app/mcp_server.py | head -10
```

Expected: no remaining `computer_use_demo` references; lines now show `from .tools import ...` etc.

- [ ] **Step 3: Update existing test import**

In `images/ubuntu/tests/test_bash.py`, line 4:

Old:
```python
from src.tools.bash import _head_tail_truncate, HEAD_BYTES, TAIL_BYTES, BashSession, BashTool
from src.tools.base import ToolResult
```

New:
```python
from app.tools.bash import _head_tail_truncate, HEAD_BYTES, TAIL_BYTES, BashSession, BashTool
from app.tools.base import ToolResult
```

- [ ] **Step 4: Run existing bash tests to verify nothing broke**

```bash
cd /data/zh/SandboxHub
pip install pytest pytest-asyncio fastapi httpx --quiet
PYTHONPATH=images/ubuntu python -m pytest images/ubuntu/tests/test_bash.py -v
```

Expected: all tests PASS (same as before migration).

- [ ] **Step 5: Commit**

```bash
git add images/ubuntu/app/ images/ubuntu/tests/
git commit -m "fix: switch router imports to relative paths for monorepo compatibility"
```

---

### Task 5: Create root main.py entry point

**Files:**
- Create: `main.py` (project root `/data/zh/SandboxHub/main.py`)
- Test: write a quick import check inline

- [ ] **Step 1: Write the failing import test**

```bash
cd /data/zh/SandboxHub
python -c "import sys; sys.path.insert(0, '.'); from src.config import settings; print(settings.SANDBOX_HUB_PORT)"
```

Expected: prints port number (e.g. `8088`). If this fails, fix PYTHONPATH first.

- [ ] **Step 2: Create root main.py**

Create `/data/zh/SandboxHub/main.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import uvicorn
from src.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=settings.SANDBOX_HUB_PORT,
        reload=False,
    )
```

- [ ] **Step 3: Verify it can be imported without error**

```bash
cd /data/zh/SandboxHub
python -c "import main; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Verify uvicorn app loads (without actually starting)**

```bash
cd /data/zh/SandboxHub
python -c "
import sys
from pathlib import Path
sys.path.insert(0, str(Path('.').resolve()))
import importlib
app_module = importlib.import_module('src.main')
print(type(app_module.app))
"
```

Expected: `<class 'fastapi.applications.FastAPI'>`

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "feat: add root main.py entry point — python main.py starts SandboxHub"
```

---

### Task 6: Add BashSession.run_stream() async generator

**Files:**
- Modify: `images/ubuntu/app/tools/bash.py`
- Create: `images/ubuntu/tests/test_bash_stream.py`

- [ ] **Step 1: Write failing tests**

Create `images/ubuntu/tests/test_bash_stream.py`:

```python
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
```

- [ ] **Step 2: Run to confirm FAIL**

```bash
cd /data/zh/SandboxHub
PYTHONPATH=images/ubuntu python -m pytest images/ubuntu/tests/test_bash_stream.py -v 2>&1 | tail -20
```

Expected: `AttributeError: 'BashSession' object has no attribute 'run_stream'`

- [ ] **Step 3: Implement run_stream() in bash.py**

In `images/ubuntu/app/tools/bash.py`, add this method to the `BashSession` class after the `run()` method (before line that defines `class BashTool`):

```python
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
```

- [ ] **Step 4: Run tests to confirm PASS**

```bash
cd /data/zh/SandboxHub
PYTHONPATH=images/ubuntu python -m pytest images/ubuntu/tests/test_bash_stream.py -v
```

Expected: 4 tests PASS.

- [ ] **Step 5: Ensure existing bash tests still pass**

```bash
PYTHONPATH=images/ubuntu python -m pytest images/ubuntu/tests/test_bash.py -v
```

Expected: all existing tests PASS.

- [ ] **Step 6: Commit**

```bash
git add images/ubuntu/app/tools/bash.py images/ubuntu/tests/test_bash_stream.py
git commit -m "feat: add BashSession.run_stream() async generator for SSE streaming"
```

---

### Task 7: Add BashTool.execute_stream() wrapper

**Files:**
- Modify: `images/ubuntu/app/tools/bash.py`
- Modify: `images/ubuntu/tests/test_bash_stream.py`

- [ ] **Step 1: Add failing test for BashTool.execute_stream()**

Append to `images/ubuntu/tests/test_bash_stream.py`:

```python
@pytest.mark.asyncio
async def test_bash_tool_execute_stream_delegates_to_session():
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
```

- [ ] **Step 2: Run to confirm FAIL**

```bash
cd /data/zh/SandboxHub
PYTHONPATH=images/ubuntu python -m pytest images/ubuntu/tests/test_bash_stream.py::test_bash_tool_execute_stream_delegates_to_session -v 2>&1 | tail -10
```

Expected: `AttributeError: 'BashTool' object has no attribute 'execute_stream'`

- [ ] **Step 3: Implement execute_stream() in BashTool**

In `images/ubuntu/app/tools/bash.py`, add this method to `BashTool` after the `execute()` method:

```python
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
```

- [ ] **Step 4: Run all stream tests to confirm PASS**

```bash
cd /data/zh/SandboxHub
PYTHONPATH=images/ubuntu python -m pytest images/ubuntu/tests/test_bash_stream.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add images/ubuntu/app/tools/bash.py images/ubuntu/tests/test_bash_stream.py
git commit -m "feat: add BashTool.execute_stream() — wraps run_stream with session lifecycle"
```

---

### Task 8: Add SSE /api/terminal/execute/stream endpoint

**Files:**
- Modify: `images/ubuntu/app/routers/terminal.py`
- Create: `images/ubuntu/tests/test_terminal_stream.py`

- [ ] **Step 1: Write failing endpoint test**

Create `images/ubuntu/tests/test_terminal_stream.py`:

```python
import json
import pytest
import asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch


async def _mock_execute_stream(command, timeout=None):
    """Fake execute_stream that yields two events."""
    yield {"type": "stdout", "chunk": "hello\n"}
    yield {"type": "done"}


@pytest.fixture
def client():
    from app.routers.terminal import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_stream_endpoint_returns_event_stream(client):
    mock_tool = MagicMock()
    mock_tool.execute_stream = _mock_execute_stream

    with patch("app.routers.terminal.get_bash_tool", return_value=mock_tool):
        resp = client.post(
            "/api/terminal/execute/stream",
            json={"command": "echo hello"},
        )

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]


def test_stream_endpoint_sse_format(client):
    mock_tool = MagicMock()
    mock_tool.execute_stream = _mock_execute_stream

    with patch("app.routers.terminal.get_bash_tool", return_value=mock_tool):
        resp = client.post(
            "/api/terminal/execute/stream",
            json={"command": "echo hello"},
        )

    data_lines = [l[5:] for l in resp.text.splitlines() if l.startswith("data:")]
    assert len(data_lines) == 2
    first = json.loads(data_lines[0])
    last = json.loads(data_lines[1])
    assert first == {"type": "stdout", "chunk": "hello\n"}
    assert last == {"type": "done"}
```

- [ ] **Step 2: Run to confirm FAIL**

```bash
cd /data/zh/SandboxHub
PYTHONPATH=images/ubuntu python -m pytest images/ubuntu/tests/test_terminal_stream.py -v 2>&1 | tail -15
```

Expected: `404 Not Found` or `ImportError` — endpoint does not exist yet.

- [ ] **Step 3: Add SSE endpoint to terminal.py**

In `images/ubuntu/app/routers/terminal.py`, add these two imports at the top of the file (after existing imports):

```python
import json
from fastapi.responses import StreamingResponse
```

Then add the new endpoint after the existing `execute_command` function:

```python
@router.post("/execute/stream", summary="流式执行 bash 命令 (SSE)")
async def execute_command_stream(request: ExecuteRequest):
    """流式执行 bash 命令，通过 SSE 实时推送输出。

    事件格式：
      data: {"type": "stdout", "chunk": "..."}
      data: {"type": "stderr", "chunk": "..."}
      data: {"type": "done"}
    """
    async def event_gen():
        async for event in get_bash_tool().execute_stream(request.command, request.timeout):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")
```

- [ ] **Step 4: Run tests to confirm PASS**

```bash
cd /data/zh/SandboxHub
PYTHONPATH=images/ubuntu python -m pytest images/ubuntu/tests/test_terminal_stream.py -v
```

Expected: both tests PASS.

- [ ] **Step 5: Run all ubuntu tests together**

```bash
PYTHONPATH=images/ubuntu python -m pytest images/ubuntu/tests/ -v
```

Expected: all tests PASS.

- [ ] **Step 6: Run SandboxHub unit tests to confirm nothing regressed**

```bash
cd /data/zh/SandboxHub
python -m pytest tests/ -v
```

Expected: all existing SandboxHub tests PASS.

- [ ] **Step 7: Commit**

```bash
git add images/ubuntu/app/routers/terminal.py images/ubuntu/tests/test_terminal_stream.py
git commit -m "feat: add POST /api/terminal/execute/stream SSE endpoint for real-time output"
```

---

## Build verification (manual)

After all tasks complete, verify the image builds with the new structure:

```bash
cd /data/zh/SandboxHub
docker build -t sandbox-ubuntu:latest images/ubuntu/
```

Expected: build completes without errors. The `scripts/` and `app/` COPY steps should succeed.
