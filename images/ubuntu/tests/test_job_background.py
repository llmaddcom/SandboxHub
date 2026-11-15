"""后台任务（SandboxHub#90）：background / detach 不回写会话状态、input、kill 的 by、结果进台账、容量已满的机器码、日志保留。

终端走真实 tmux + bash；进程活性用可控的假扫描（同 test_job_origin）。
"""

import json
import os
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import terminal
from app.tools import bash as bash_mod
from app.tools.bash import BashTool

ORIGIN = {"X-Sandbox-Job-Origin": json.dumps({"session_id": "s1", "turn_id": "t1"})}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(bash_mod, "scan_marked_processes", lambda: [])
    monkeypatch.setattr(terminal, "scan_marked_processes", lambda: [])
    monkeypatch.setattr(bash_mod, "END_POLL_INTERVAL", 0.05)
    monkeypatch.setattr(terminal, "bash_tool", BashTool())
    app = FastAPI()
    app.include_router(terminal.router)
    with TestClient(app) as c:
        yield c


def _run(client, command: str, **extra) -> dict:
    response = client.post(
        "/api/terminal/execute", json={"command": command, "wait": 5, **extra}, headers=ORIGIN
    )
    assert response.status_code == 200, response.text
    return response.json()


def _wait(client, job_id: str, wait: float = 5) -> dict:
    response = client.post("/api/terminal/wait", json={"job_id": job_id, "wait": wait})
    assert response.status_code == 200, response.text
    return response.json()


def _ledger_entry(client, job_id: str) -> dict:
    for _ in range(100):
        jobs = client.get("/api/terminal/activity").json()["jobs"]
        entry = next(e for e in jobs if e["job_id"] == job_id)
        if entry["ended_at"] is not None:
            return entry
        time.sleep(0.05)
    raise AssertionError("job 未结束")


def _pwd(client) -> str:
    return _run(client, "pwd; echo \"$CR_T\"")["output"]


def test_background_job_returns_at_once_and_never_writes_session_state_back(client, tmp_path):
    _run(client, "cd /usr && export CR_T=front")
    started = time.monotonic()
    job = client.post(
        "/api/terminal/execute",
        json={"command": f"cd {tmp_path} && export CR_T=back && sleep 0.4", "background": True},
        headers=ORIGIN,
    ).json()  # 不带 wait 也不走旧契约的阻塞
    assert time.monotonic() - started < 0.35
    assert job["status"] == "running" and job["detached"] is True
    # 后台任务在跑时前台改了状态；它结束后不得把自己启动时的旧快照盖回来
    _run(client, "cd /var && export CR_T=newer")
    done = _wait(client, job["job_id"])
    assert done["status"] == "exited" and done["exit_code"] == 0 and done["detached"] is True
    assert _pwd(client) == "/var\nnewer\n"


def test_foreground_job_still_writes_session_state_back(client, tmp_path):
    job = _run(client, f"cd {tmp_path} && export CR_T=kept")
    assert job["detached"] is False
    assert _pwd(client) == f"{os.path.realpath(tmp_path)}\nkept\n"


def test_detach_marks_running_job_and_is_idempotent(client, tmp_path):
    _run(client, "cd /usr")
    job = _run(client, f"cd {tmp_path} && sleep 0.4", wait=0)
    assert job["status"] == "running" and job["detached"] is False
    for _ in range(2):
        response = client.post("/api/terminal/detach", json={"job_id": job["job_id"]})
        assert response.status_code == 200 and response.json()["detached"] is True
    assert _wait(client, job["job_id"])["status"] == "exited"
    assert _pwd(client).startswith("/usr\n")
    # 已结束的 job：无操作，不报错
    assert client.post("/api/terminal/detach", json={"job_id": job["job_id"]}).status_code == 200
    assert client.post("/api/terminal/detach", json={"job_id": "j_nope"}).status_code == 404


def test_input_feeds_an_interactive_job(client):
    job = _run(client, "read -r name; echo \"got:$name\"", wait=0.3)
    assert job["status"] == "running"
    response = client.post("/api/terminal/input", json={"job_id": job["job_id"], "text": "Enter C-c $HOME"})
    assert response.status_code == 200
    done = _wait(client, job["job_id"])
    # 文本按字面送入：不解释成按键名，也不由我方展开
    assert done["status"] == "exited" and done["exit_code"] == 0
    assert "got:Enter C-c $HOME" in _wait(client, job["job_id"], 0)["output"] or "got:Enter C-c $HOME" in done["output"]


def test_input_without_enter_then_enter_only(client):
    job = _run(client, "read -r name; echo \"got:$name\"", wait=0.3)
    client.post("/api/terminal/input", json={"job_id": job["job_id"], "text": "ab", "enter": False})
    assert _wait(client, job["job_id"], 0.2)["status"] == "running"
    out = client.post("/api/terminal/input", json={"job_id": job["job_id"], "text": "c"}).json()
    done = _wait(client, job["job_id"])
    assert done["status"] == "exited"
    assert "got:abc" in (out["output"] + client.post(
        "/api/terminal/wait", json={"job_id": job["job_id"], "cursor": 0, "wait": 0}
    ).json()["output"])


def test_input_rejects_finished_and_unknown_jobs(client):
    job = _run(client, "true")
    assert client.post("/api/terminal/input", json={"job_id": job["job_id"], "text": "x"}).status_code == 409
    assert client.post("/api/terminal/input", json={"job_id": "j_nope", "text": "x"}).status_code == 404


def test_kill_records_who_stopped_the_job_and_ledger_reports_it(client):
    job = _run(client, "sleep 30", wait=0, background=True)
    killed = client.post(
        "/api/terminal/kill", json={"job_id": job["job_id"], "signal": "KILL", "by": "user"}
    ).json()
    assert killed["status"] == "killed" and killed["killed_by"] == "user"
    # 先到先得：之后别人再 kill 不改记录
    client.post("/api/terminal/kill", json={"job_id": job["job_id"], "by": "model"})
    entry = _ledger_entry(client, job["job_id"])
    assert entry["killed_by"] == "user" and entry["kill_reason"] == "kill:KILL"
    assert entry["detached"] is True and entry["exit_code"] is not None
    bad = client.post("/api/terminal/kill", json={"job_id": job["job_id"], "by": "root"})
    assert bad.status_code == 422


def test_ledger_reports_exit_code_and_survives_losing_the_in_memory_record(client, monkeypatch):
    job = _run(client, "exit 3")
    assert job["exit_code"] == 3
    entry = _ledger_entry(client, job["job_id"])
    assert (entry["exit_code"], entry["kill_reason"], entry["killed_by"], entry["detached"]) == (3, None, None, False)
    assert _wait(client, job["job_id"], 0)["ended_at"] == entry["ended_at"]
    # 操作服务重启 / 内存记录被淘汰后，结果仍从落盘文件回报
    monkeypatch.setattr(terminal, "bash_tool", BashTool())
    again = next(e for e in client.get("/api/terminal/activity").json()["jobs"] if e["job_id"] == job["job_id"])
    assert again["exit_code"] == 3 and again["ended_at"] == entry["ended_at"]


def test_running_job_has_no_result_yet(client):
    job = _run(client, "sleep 30", wait=0)
    entry = next(e for e in client.get("/api/terminal/activity").json()["jobs"] if e["job_id"] == job["job_id"])
    assert entry["ended_at"] is None and entry["exit_code"] is None and entry["kill_reason"] is None
    client.post("/api/terminal/kill", json={"job_id": job["job_id"], "signal": "KILL"})
    _wait(client, job["job_id"])


def test_full_capacity_returns_stable_code_then_frees_up(client, monkeypatch):
    monkeypatch.setattr(bash_mod, "MAX_LIVE_WINDOWS", 2)
    first = _run(client, "sleep 30", wait=0)
    second = _run(client, "sleep 30", wait=0)
    response = client.post("/api/terminal/execute", json={"command": "echo no", "wait": 1})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "job_limit_reached" and detail["limit"] == 2 and detail["running"] == 2
    assert "新命令未执行" in detail["message"]
    # 已有任务不受影响；结束一个后（它的退出窗口在提交前被清掉）即可再提交
    assert _wait(client, second["job_id"], 0)["status"] == "running"
    client.post("/api/terminal/kill", json={"job_id": first["job_id"], "signal": "KILL"})
    _wait(client, first["job_id"])
    assert _run(client, "echo ok")["output"] == "ok\n"
    client.post("/api/terminal/kill", json={"job_id": second["job_id"], "signal": "KILL"})
    _wait(client, second["job_id"])


def test_capacity_zero_means_unlimited(client, monkeypatch):
    monkeypatch.setattr(bash_mod, "MAX_LIVE_WINDOWS", 0)
    jobs = [_run(client, "sleep 30", wait=0) for _ in range(3)]
    assert all(job["status"] == "running" for job in jobs)
    for job in jobs:
        client.post("/api/terminal/kill", json={"job_id": job["job_id"], "signal": "KILL"})
        _wait(client, job["job_id"])


def test_log_retention_sweeps_only_old_ended_jobs(client, monkeypatch):
    old = _run(client, "echo old")
    live = _run(client, "sleep 30", wait=0)
    _ledger_entry(client, old["job_id"])
    log = bash_mod.JOB_DIR / f"{old['job_id']}.log"
    # 默认 0 = 不清理
    client.get("/api/terminal/activity")
    assert log.exists()
    past = time.time() - 100
    os.utime(log, (past, past))
    os.utime(bash_mod.JOB_DIR / f"{live['job_id']}.log", (past, past))
    monkeypatch.setattr(bash_mod, "LOG_RETENTION", 50.0)
    activity = client.get("/api/terminal/activity").json()
    assert not log.exists() and (bash_mod.JOB_DIR / f"{live['job_id']}.log").exists()
    # 日志清了，任务清单里的记录（来源与结果）按自己的保留期还在
    assert next(e for e in activity["jobs"] if e["job_id"] == old["job_id"])["exit_code"] == 0
    client.post("/api/terminal/kill", json={"job_id": live["job_id"], "signal": "KILL"})
    _wait(client, live["job_id"])
