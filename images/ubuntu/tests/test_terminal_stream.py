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
