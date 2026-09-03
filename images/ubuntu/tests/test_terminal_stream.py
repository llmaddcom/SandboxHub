"""终端路由（/api/terminal/*）——job 契约 + 旧契约 + SSE，走真实 bash。"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import terminal
from app.tools.bash import BashTool


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(terminal, "bash_tool", BashTool())
    app = FastAPI()
    app.include_router(terminal.router)
    with TestClient(app) as c:
        yield c


# ── job 契约 ─────────────────────────────────────────────────────────────────

def test_execute_with_wait_returns_running_then_wait_until_exited(client):
    resp = client.post("/api/terminal/execute", json={"command": "sleep 0.6; echo done", "wait": 0.1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "running"
    assert body["exit_code"] is None
    assert body["job_id"].startswith("j_")
    assert body["log_path"].endswith(f"{body['job_id']}.log")
    assert body["success"] is True

    final = None
    for _ in range(20):
        r = client.post("/api/terminal/wait", json={"job_id": body["job_id"], "cursor": body["cursor"], "wait": 0.3})
        assert r.status_code == 200
        final = r.json()
        if final["status"] != "running":
            break
    assert final["status"] == "exited"
    assert final["exit_code"] == 0
    assert "done" in final["output"]


def test_execute_wait_zero_returns_immediately(client):
    body = client.post("/api/terminal/execute", json={"command": "sleep 1", "wait": 0}).json()
    assert body["status"] == "running"
    client.post("/api/terminal/kill", json={"job_id": body["job_id"], "signal": "KILL"})


def test_execute_without_timeout_has_no_default_limit(client):
    body = client.post("/api/terminal/execute", json={"command": "echo ok", "wait": 5, "timeout": None}).json()
    assert body["status"] == "exited"
    assert body["kill_reason"] is None
    assert body["output"] == "ok\n"


def test_execute_timeout_kills_with_reason(client):
    body = client.post("/api/terminal/execute", json={"command": "sleep 30", "wait": 5, "timeout": 0.3}).json()
    assert body["status"] == "killed"
    assert body["kill_reason"] == "timeout"
    assert body["exit_code"] == 130


def test_execute_timeout_has_no_upper_bound(client):
    body = client.post("/api/terminal/execute", json={"command": "echo ok", "wait": 5, "timeout": 36000}).json()
    assert body["status"] == "exited"


def test_state_persists_across_execute_calls(client, tmp_path):
    client.post("/api/terminal/execute", json={"command": f"cd {tmp_path} && export FOO=1", "wait": 5})
    body = client.post("/api/terminal/execute", json={"command": "pwd; echo $FOO", "wait": 5}).json()
    assert body["output"] == f"{tmp_path.resolve()}\n1\n"


def test_execute_while_busy_returns_409_with_running_job(client):
    running = client.post("/api/terminal/execute", json={"command": "sleep 30", "wait": 0}).json()
    resp = client.post("/api/terminal/execute", json={"command": "echo x", "wait": 0})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["job_id"] == running["job_id"]
    assert detail["status"] == "running"
    client.post("/api/terminal/kill", json={"job_id": running["job_id"], "signal": "KILL"})


def test_kill_then_wait_returns_killed_and_session_continues(client):
    running = client.post("/api/terminal/execute", json={"command": "sleep 30", "wait": 0.1}).json()
    resp = client.post("/api/terminal/kill", json={"job_id": running["job_id"], "signal": "INT"})
    assert resp.status_code == 200
    waited = client.post("/api/terminal/wait", json={"job_id": running["job_id"], "wait": 5}).json()
    assert waited["status"] == "killed"
    assert waited["exit_code"] == 130
    assert waited["kill_reason"] == "kill:INT"
    nxt = client.post("/api/terminal/execute", json={"command": "echo after", "wait": 5}).json()
    assert nxt["status"] == "exited"
    assert nxt["output"] == "after\n"


def test_kill_default_signal_is_int(client):
    running = client.post("/api/terminal/execute", json={"command": "sleep 30", "wait": 0}).json()
    body = client.post("/api/terminal/kill", json={"job_id": running["job_id"]}).json()
    assert body["kill_reason"] == "kill:INT"


def test_kill_invalid_signal_returns_400(client):
    running = client.post("/api/terminal/execute", json={"command": "sleep 30", "wait": 0}).json()
    resp = client.post("/api/terminal/kill", json={"job_id": running["job_id"], "signal": "HUP"})
    assert resp.status_code == 400
    client.post("/api/terminal/kill", json={"job_id": running["job_id"], "signal": "KILL"})


def test_wait_and_kill_unknown_job_return_404(client):
    assert client.post("/api/terminal/wait", json={"job_id": "j_nope"}).status_code == 404
    assert client.post("/api/terminal/kill", json={"job_id": "j_nope"}).status_code == 404


def test_restart_kills_running_job_and_resets_cwd(client, tmp_path):
    client.post("/api/terminal/execute", json={"command": f"cd {tmp_path}", "wait": 5})
    running = client.post("/api/terminal/execute", json={"command": "sleep 30", "wait": 0}).json()
    resp = client.post("/api/terminal/restart")
    assert resp.status_code == 200 and resp.json()["success"] is True
    waited = client.post("/api/terminal/wait", json={"job_id": running["job_id"], "wait": 5}).json()
    assert waited["status"] == "killed"
    assert waited["kill_reason"] == "restart"
    body = client.post("/api/terminal/execute", json={"command": "pwd", "wait": 5}).json()
    assert body["output"].strip() != str(tmp_path.resolve())


def test_execute_validation_errors(client):
    assert client.post("/api/terminal/execute", json={"command": "x", "wait": -1}).status_code == 422
    assert client.post("/api/terminal/execute", json={"command": "x", "wait": 1, "timeout": 0}).status_code == 422
    assert client.post("/api/terminal/execute", json={"command": "", "wait": 1}).status_code == 400


# ── 旧契约 ───────────────────────────────────────────────────────────────────

def test_legacy_execute_blocks_and_returns_old_shape(client):
    body = client.post("/api/terminal/execute", json={"command": "sleep 0.3; echo hi", "timeout": 10}).json()
    assert body["success"] is True
    assert body["output"] == "hi\n"
    assert body["error"] == ""
    assert body["system"] is None
    # 新字段也在，便于混用
    assert body["status"] == "exited" and body["exit_code"] == 0


def test_legacy_execute_timeout_notice(client, monkeypatch):
    body = client.post("/api/terminal/execute", json={"command": "sleep 30", "timeout": 0.3}).json()
    assert "超时" in body["output"]
    assert body["status"] == "killed"


def test_legacy_execute_default_timeout_is_30s(client, monkeypatch):
    from app.tools import bash as bash_mod
    monkeypatch.setattr(bash_mod, "DEFAULT_TIMEOUT", 0.3)
    body = client.post("/api/terminal/execute", json={"command": "sleep 30"}).json()
    assert "0.3" in body["output"]
    assert body["kill_reason"] == "timeout"


# ── SSE ──────────────────────────────────────────────────────────────────────

def _sse_events(text: str) -> list[dict]:
    return [json.loads(l[5:]) for l in text.splitlines() if l.startswith("data:")]


def test_stream_endpoint_returns_event_stream(client):
    resp = client.post("/api/terminal/execute/stream", json={"command": "echo hello"})
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]


def test_stream_endpoint_sse_format(client):
    resp = client.post("/api/terminal/execute/stream", json={"command": "echo hello"})
    events = _sse_events(resp.text)
    assert events == [{"type": "stdout", "chunk": "hello\n"}, {"type": "done"}]
