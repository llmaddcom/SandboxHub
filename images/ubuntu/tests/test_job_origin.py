"""任务来源台账（SandboxHub#47）：受信头打标、起止时间、后台后代存活期间不结束、无来源存活任务、保留期清理。

进程活性用可控的假扫描（真实 /proc 在宿主上读不全）；终端走真实 tmux + bash。
"""

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import terminal
from app.tools import bash as bash_mod
from app.tools.bash import BashTool

ORIGIN = {"X-Sandbox-Job-Origin": json.dumps({"session_id": "s1", "turn_id": "t1"})}


class Alive(set):
    """假进程扫描的存活集合；``hold_all`` 时所有已落来源的 job 都有存活后代。"""

    hold_all = False

    def scan(self) -> list[str]:
        ids = set(self)
        if self.hold_all:
            ids |= {p.name[: -len(".origin")] for p in bash_mod.JOB_DIR.glob("j_*.origin")}
        return sorted(ids)


@pytest.fixture
def alive(monkeypatch):
    """假进程扫描：返回集合里的 job ID（每个代表一个带标记的存活进程）。"""
    ids = Alive()
    scan = ids.scan
    monkeypatch.setattr(bash_mod, "scan_marked_processes", scan)
    monkeypatch.setattr(terminal, "scan_marked_processes", scan)
    monkeypatch.setattr(bash_mod, "END_POLL_INTERVAL", 0.05)
    return ids


@pytest.fixture
def client(monkeypatch, alive):
    monkeypatch.setattr(terminal, "bash_tool", BashTool())
    app = FastAPI()
    app.include_router(terminal.router)
    with TestClient(app) as c:
        yield c


def _activity(client) -> dict:
    response = client.get("/api/terminal/activity")
    assert response.status_code == 200
    return response.json()


def _entry(client, job_id: str) -> dict:
    return next(e for e in _activity(client)["jobs"] if e["job_id"] == job_id)


def _wait_ended(client, job_id: str) -> dict:
    for _ in range(100):
        entry = _entry(client, job_id)
        if entry["ended_at"] is not None:
            return entry
        time.sleep(0.05)
    raise AssertionError("job 未结束")


def test_origin_header_tags_job_and_records_start_and_end(client):
    before = time.time()
    job = client.post(
        "/api/terminal/execute", json={"command": "echo hi", "wait": 5}, headers=ORIGIN
    ).json()
    assert job["status"] == "exited"
    entry = _wait_ended(client, job["job_id"])
    assert (entry["session_id"], entry["turn_id"]) == ("s1", "t1")
    assert before <= entry["started_at"] <= entry["ended_at"] <= time.time()


@pytest.mark.parametrize("header", [None, "not json", '{"session_id": 1, "turn_id": "t"}', "[]"])
def test_missing_or_malformed_origin_is_recorded_as_unknown(client, header):
    headers = {} if header is None else {"X-Sandbox-Job-Origin": header}
    job = client.post("/api/terminal/execute", json={"command": "true", "wait": 5}, headers=headers).json()
    entry = _entry(client, job["job_id"])
    assert entry["session_id"] is None and entry["turn_id"] is None


def test_running_job_is_open_and_legacy_contract_is_tagged_too(client):
    running = client.post(
        "/api/terminal/execute", json={"command": "sleep 30", "wait": 0}, headers=ORIGIN
    ).json()
    assert _entry(client, running["job_id"])["ended_at"] is None
    legacy = client.post("/api/terminal/execute", json={"command": "true"}, headers=ORIGIN).json()
    assert _entry(client, legacy["job_id"])["turn_id"] == "t1"
    client.post("/api/terminal/kill", json={"job_id": running["job_id"], "signal": "KILL"})
    client.post("/api/terminal/wait", json={"job_id": running["job_id"], "wait": 5})
    _wait_ended(client, running["job_id"])


def test_stream_contract_is_tagged(client):
    with client.stream(
        "POST", "/api/terminal/execute/stream", json={"command": "echo s"}, headers=ORIGIN
    ) as response:
        assert response.status_code == 200
        "".join(response.iter_text())
    [entry] = _activity(client)["jobs"]
    assert entry["session_id"] == "s1"


def test_background_descendant_keeps_job_open_until_it_exits(client, alive):
    alive.hold_all = True  # 窗口结束后仍有带该 job 标记的后代进程（nohup / setsid）
    job = client.post(
        "/api/terminal/execute", json={"command": "true", "wait": 5}, headers=ORIGIN
    ).json()
    assert job["status"] == "exited"
    time.sleep(0.3)
    assert _entry(client, job["job_id"])["ended_at"] is None
    terminal.bash_tool.session.jobs.clear()  # 内存记录被裁掉也不丢
    body = _activity(client)
    assert body["marked_processes"] == 1 and body["unattributed_live"] == []
    assert _entry(client, job["job_id"])["ended_at"] is None
    gone_at = time.time()
    alive.hold_all = False
    entry = _wait_ended(client, job["job_id"])
    assert entry["ended_at"] >= gone_at


def test_end_tracker_records_descendant_exit_without_queries(client, alive):
    alive.hold_all = True
    job = client.post(
        "/api/terminal/execute", json={"command": "true", "wait": 5}, headers=ORIGIN
    ).json()
    time.sleep(0.2)
    gone_at = time.time()
    alive.hold_all = False
    time.sleep(0.3)  # 后台追踪结算，不靠查询时刻
    entry = _entry(client, job["job_id"])
    queried_at = time.time()
    assert gone_at <= entry["ended_at"] < queried_at


def test_live_process_without_origin_record_is_unattributed(client, alive):
    alive.add("j_01ARZ3NDEKTSV4RRFFQ69G5FAV")
    body = _activity(client)
    assert body["unattributed_live"] == ["j_01ARZ3NDEKTSV4RRFFQ69G5FAV"] and body["jobs"] == []


def test_entries_past_retention_are_dropped(client, monkeypatch):
    job = client.post(
        "/api/terminal/execute", json={"command": "true", "wait": 5}, headers=ORIGIN
    ).json()
    _wait_ended(client, job["job_id"])
    origin_file = bash_mod.JOB_DIR / f"{job['job_id']}.origin"
    assert origin_file.exists()
    monkeypatch.setattr(bash_mod, "LEDGER_RETENTION", -1.0)
    assert _activity(client)["jobs"] == []
    assert not origin_file.exists()
