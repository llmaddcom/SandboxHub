"""长驻进程（/api/procs，SandboxHub#52）：stdio 往返、stdout/stderr 分路、游标续读、背压、
退出与信号、环境隔离、启动前文件与 reset_dirs、同名冲突、日志保留、长轮询、活性计数、
进程不随请求 / 客户端结束。走真实子进程（cat / sh / env / python）。"""

import asyncio
import base64
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import procs as procs_router
from app.routers import terminal
from app.tools import bash as bash_mod
from app.tools import procs as procs_mod
from app.tools.bash import BashTool
from app.tools.procs import ProcManager


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(procs_mod, "PROC_DIR", tmp_path / "cr-procs")
    monkeypatch.setattr(procs_mod, "DRAIN_GRACE", 0.5)
    # 活性扫描只看本测试造的进程条目（宿主上别人的 /proc 读不全）
    process_root = tmp_path / "proc"
    process_root.mkdir()
    monkeypatch.setattr(bash_mod, "PROC_ROOT", process_root)
    procs = ProcManager()
    monkeypatch.setattr(procs_router, "proc_manager", procs)
    yield procs
    procs.close()


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(procs_router.router)
    app.include_router(terminal.router)
    return app


@pytest.fixture
def client(manager, monkeypatch):
    monkeypatch.setattr(terminal, "bash_tool", BashTool())
    with TestClient(_app()) as c:
        yield c


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _start(client, name="p", **body) -> dict:
    response = client.post("/api/procs", json={"name": name, **body})
    assert response.status_code == 201, response.text
    return response.json()


def _write(client, proc_id, data=b"", close=False) -> dict:
    response = client.post(f"/api/procs/{proc_id}/stdin", json={"data_b64": _b64(data), "close": close})
    assert response.status_code == 200, response.text
    return response.json()


def _read(client, proc_id, out=0, err=0, wait=5.0, max_bytes=0) -> dict:
    response = client.post(
        f"/api/procs/{proc_id}/read",
        json={"stdout_cursor": out, "stderr_cursor": err, "wait": wait, "max_bytes": max_bytes},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _drain(client, proc_id, *, max_bytes=0, timeout=15.0) -> tuple[bytes, bytes, dict]:
    """从 0 读到进程结束且两路读尽。"""
    out, err, out_cursor, err_cursor = b"", b"", 0, 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = _read(client, proc_id, out_cursor, err_cursor, wait=2.0, max_bytes=max_bytes)
        out += base64.b64decode(body["stdout_b64"])
        err += base64.b64decode(body["stderr_b64"])
        out_cursor, err_cursor, info = body["stdout_cursor"], body["stderr_cursor"], body["proc"]
        assert not body["truncated"]
        if info["state"] != "running" and out_cursor >= info["stdout_size"] and err_cursor >= info["stderr_size"]:
            return out, err, info
    raise AssertionError("进程未在时限内结束")


def _read_stdout(client, proc_id, size, cursor=0, max_bytes=0) -> bytes:
    """读 stdout 直到拿到 size 字节（进程不必结束）。"""
    data = b""
    deadline = time.monotonic() + 10
    while len(data) < size and time.monotonic() < deadline:
        body = _read(client, proc_id, cursor, wait=2.0, max_bytes=max_bytes)
        data += base64.b64decode(body["stdout_b64"])
        cursor = body["stdout_cursor"]
    return data


def _wait_ended(client, proc_id) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        info = _read(client, proc_id, wait=2.0, max_bytes=1)["proc"]
        if info["state"] != "running":
            return info
    raise AssertionError("进程未结束")


def _gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return True
        if state in ("Z", "X"):
            return True
        time.sleep(0.05)
    return False


# ── stdio ─────────────────────────────────────────────────────────────────────


def test_cat_roundtrip_is_byte_identical_across_split_writes(client):
    proc = _start(client, argv=["cat"])
    assert proc["proc_id"].startswith("p_") and proc["state"] == "running" and proc["stdin_open"]
    payload = "héllo 世界\n".encode() + bytes(range(256))
    split = payload.index("世".encode()) + 1   # 切在多字节字符中间
    for part in (payload[:split], payload[split:]):
        assert _write(client, proc["proc_id"], part) == {"written": len(part), "stdin_open": True}
    assert _read_stdout(client, proc["proc_id"], len(payload)) == payload


def test_large_write_is_fully_delivered_through_the_pipe(client):
    proc = _start(client, argv=["cat"])
    payload = os.urandom(1 << 20)   # 远大于管道缓冲：写入要分多次、等子进程读走
    assert _write(client, proc["proc_id"], payload)["written"] == len(payload)
    assert _read_stdout(client, proc["proc_id"], len(payload)) == payload


def test_stdout_and_stderr_are_separate_streams(client):
    proc = _start(client, argv=["sh", "-c", "printf out; printf err >&2; printf more"])
    out, err, info = _drain(client, proc["proc_id"])
    assert out == b"outmore" and err == b"err"
    assert info["state"] == "exited" and info["exit_code"] == 0
    assert info["stdout_size"] == 7 and info["stderr_size"] == 3


def test_small_reads_resume_from_cursors_without_losing_bytes(client):
    script = "import sys; sys.stdout.buffer.write(bytes((i * 7) % 251 for i in range(3000)))"
    expected = bytes((i * 7) % 251 for i in range(3000))
    proc = _start(client, argv=[sys.executable, "-c", script])
    out, err, info = _drain(client, proc["proc_id"], max_bytes=97)
    assert out == expected and err == b"" and info["exit_code"] == 0
    # 断线重连：任意游标处重新读，得到的正是该偏移之后的字节
    body = _read(client, proc["proc_id"], out=1234, max_bytes=10)
    assert base64.b64decode(body["stdout_b64"]) == expected[1234:1244] and body["stdout_cursor"] == 1244


def test_backpressure_blocks_writer_until_the_process_exits(client):
    proc = _start(client, argv=["sleep", "30"])   # 从不读 stdin
    result = {}

    def write():
        result["response"] = client.post(
            f"/api/procs/{proc['proc_id']}/stdin", json={"data_b64": _b64(b"x" * (1 << 20))}
        )

    writer = threading.Thread(target=write)
    writer.start()
    time.sleep(0.5)
    assert writer.is_alive()   # 管道满：写入方等着（背压），不丢不报错
    client.post(f"/api/procs/{proc['proc_id']}/signal", json={"signal": "KILL"})
    writer.join(5)
    assert not writer.is_alive()
    assert result["response"].status_code == 409
    assert result["response"].json()["detail"]["reason"] == "proc_exited"


# ── 结束与信号 ─────────────────────────────────────────────────────────────────


def test_closing_stdin_lets_cat_exit_with_its_exit_code(client):
    proc = _start(client, argv=["cat"])
    assert _write(client, proc["proc_id"], b"bye", close=True) == {"written": 3, "stdin_open": False}
    out, _, info = _drain(client, proc["proc_id"])
    assert out == b"bye"
    assert info["state"] == "exited" and info["exit_code"] == 0 and info["ended_at"].endswith("Z")
    assert info["stdin_open"] is False


def test_stdin_errors_after_close_and_after_exit(client):
    proc = _start(client, argv=["sleep", "30"])
    _write(client, proc["proc_id"], close=True)
    response = client.post(f"/api/procs/{proc['proc_id']}/stdin", json={"data_b64": _b64(b"x")})
    assert response.status_code == 409 and response.json()["detail"]["reason"] == "stdin_closed"
    client.post(f"/api/procs/{proc['proc_id']}/signal", json={"signal": "KILL"})
    _wait_ended(client, proc["proc_id"])
    response = client.post(f"/api/procs/{proc['proc_id']}/stdin", json={"data_b64": _b64(b"x")})
    assert response.status_code == 409 and response.json()["detail"]["reason"] == "proc_exited"
    assert _write(client, proc["proc_id"], close=True) == {"written": 0, "stdin_open": False}  # 幂等
    response = client.post(f"/api/procs/{proc['proc_id']}/stdin", json={"data_b64": "%%%"})
    assert response.status_code == 400 and response.json()["detail"]["reason"] == "invalid_request"


def test_signal_reaches_the_process_group_and_reports_killed(client):
    proc = _start(client, argv=["sh", "-c", "sleep 30 & echo $!; wait"])
    child = int(_read_stdout(client, proc["proc_id"], 2).decode().split()[0])
    assert client.post(f"/api/procs/{proc['proc_id']}/signal", json={"signal": "TERM"}).json() == {"ok": True}
    info = _wait_ended(client, proc["proc_id"])
    assert info["state"] == "killed" and info["exit_code"] == -15
    assert _gone(child)   # 同组的后台子进程也收到了信号
    # 已结束：再发信号无操作
    assert client.post(f"/api/procs/{proc['proc_id']}/signal", json={"signal": "KILL"}).status_code == 200
    response = client.post(f"/api/procs/{proc['proc_id']}/signal", json={"signal": "USR1"})
    assert response.status_code == 400 and response.json()["detail"]["reason"] == "invalid_request"


def test_death_by_a_signal_we_did_not_send_is_exited_with_negative_code(client):
    proc = _start(client, argv=["sh", "-c", "kill -9 $$"])
    info = _wait_ended(client, proc["proc_id"])
    assert info["state"] == "exited" and info["exit_code"] == -9


def test_background_child_holding_stdout_does_not_hold_the_exit(client):
    proc = _start(client, argv=["sh", "-c", "(sleep 1; echo late) & echo early"])
    out, _, info = _drain(client, proc["proc_id"])
    assert out == b"early\n" and info["state"] == "exited" and info["exit_code"] == 0
    time.sleep(1.3)
    # 结束后大小定格：后代晚到的输出不再记入
    assert client.get(f"/api/procs/{proc['proc_id']}").json()["stdout_size"] == len(b"early\n")


def test_delete_kills_a_running_group_and_removes_record_and_logs(client):
    proc = _start(client, argv=["sh", "-c", "sleep 30 & echo $!; wait"])
    child = int(_read_stdout(client, proc["proc_id"], 2).decode().split()[0])
    log_dir = procs_mod.PROC_DIR / proc["proc_id"]
    assert (log_dir / "stdout").exists() and (log_dir / "stderr").exists()
    assert client.delete(f"/api/procs/{proc['proc_id']}").json() == {"ok": True}
    assert not log_dir.exists() and _gone(proc["pid"]) and _gone(child)
    assert client.get("/api/procs").json() == {"procs": []}


def test_unknown_proc_is_404(client):
    missing = "p_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    for response in (
        client.get(f"/api/procs/{missing}"),
        client.post(f"/api/procs/{missing}/read", json={}),
        client.post(f"/api/procs/{missing}/stdin", json={"data_b64": ""}),
        client.post(f"/api/procs/{missing}/signal", json={"signal": "KILL"}),
        client.delete(f"/api/procs/{missing}"),
    ):
        assert response.status_code == 404 and response.json()["detail"]["reason"] == "proc_not_found"


# ── 启动 ──────────────────────────────────────────────────────────────────────


def test_env_is_explicit_and_never_inherits_the_api_token(client, monkeypatch):
    monkeypatch.setenv("SANDBOX_API_TOKEN", "secret-token")
    monkeypatch.setenv("CR_TEST_INHERITED", "from-app")
    monkeypatch.setenv("CR_TEST_OVERRIDDEN", "from-app")
    proc = _start(
        client,
        argv=["env"],
        env={"FOO": "bar", "CR_TEST_OVERRIDDEN": "explicit"},
        inherit_env=["CR_TEST_INHERITED", "CR_TEST_OVERRIDDEN", "CR_TEST_MISSING"],
    )
    out, _, _ = _drain(client, proc["proc_id"])
    env = dict(line.split("=", 1) for line in out.decode().splitlines())
    assert env == {
        "FOO": "bar",
        "CR_TEST_INHERITED": "from-app",
        "CR_TEST_OVERRIDDEN": "explicit",
        "CR_SANDBOX_PROC_ID": proc["proc_id"],
    }


def test_files_are_written_with_mode_before_start(client, tmp_path):
    target = tmp_path / "home" / "deep" / "conf.json"
    existing = tmp_path / "home" / "old.txt"
    existing.parent.mkdir()
    existing.write_text("old")
    proc = _start(
        client,
        argv=["cat", str(target), str(existing)],
        files=[
            {"path": str(target), "content_b64": _b64(b'{"a":1}'), "mode": 0o640},
            {"path": str(existing), "content_b64": _b64(b"new")},
        ],
    )
    out, _, info = _drain(client, proc["proc_id"])
    assert out == b'{"a":1}new' and info["exit_code"] == 0
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert stat.S_IMODE(existing.stat().st_mode) == 0o600   # 缺省权限
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700   # 新建的上级目录不对外开放


def test_name_conflict_and_replacement_of_an_ended_proc(client):
    first = _start(client, name="engine", argv=["sleep", "30"])
    response = client.post("/api/procs", json={"name": "engine", "argv": ["true"]})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason"] == "proc_exists" and detail["proc_id"] == first["proc_id"]
    client.post(f"/api/procs/{first['proc_id']}/signal", json={"signal": "KILL"})
    _wait_ended(client, first["proc_id"])
    old_logs = procs_mod.PROC_DIR / first["proc_id"]
    assert old_logs.exists()
    second = _start(client, name="engine", argv=["true"])
    assert second["proc_id"] != first["proc_id"]
    assert client.get(f"/api/procs/{first['proc_id']}").status_code == 404 and not old_logs.exists()
    listed = client.get("/api/procs", params={"name": "engine"}).json()["procs"]
    assert [p["proc_id"] for p in listed] == [second["proc_id"]]
    other = _start(client, name="other", argv=["true"])
    assert {p["proc_id"] for p in client.get("/api/procs").json()["procs"]} == {second["proc_id"], other["proc_id"]}


@pytest.mark.parametrize(
    "body",
    [
        {"name": "x", "argv": []},
        {"name": "", "argv": ["true"]},
        {"name": "x"},
        {"name": "x", "argv": ["/nonexistent/binary"]},
        {"name": "x", "argv": ["true"], "env": {"CR_SANDBOX_PROC_ID": "p_x"}},
        {"name": "x", "argv": ["true"], "inherit_env": ["CR_SANDBOX_JOB_ID"]},
        {"name": "x", "argv": ["true"], "env": {"BAD-NAME": "1"}},
        {"name": "x", "argv": ["true"], "cwd": "relative"},
        {"name": "x", "argv": ["true"], "cwd": "/nonexistent-dir-for-procs-test"},
        {"name": "x", "argv": ["true"], "files": [{"path": "rel.txt", "content_b64": ""}]},
        {"name": "x", "argv": ["true"], "files": [{"path": "/tmp/procs-test-x", "content_b64": "%%%"}]},
        {"name": "x", "argv": ["true"], "log_retain_bytes": -1},
    ],
)
def test_invalid_start_requests_are_400(client, body):
    response = client.post("/api/procs", json=body)
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["reason"] == "invalid_request"
    assert client.get("/api/procs").json() == {"procs": []}


def test_malformed_json_body_is_400(client):
    response = client.post("/api/procs", content=b"not json", headers={"content-type": "application/json"})
    assert response.status_code == 400 and response.json()["detail"]["reason"] == "invalid_request"


# ── reset_dirs ────────────────────────────────────────────────────────────────


def test_reset_dirs_wipes_planted_files_before_files_are_written(client, tmp_path):
    home = tmp_path / "engine-home" / "s1"
    (home / "nested").mkdir(parents=True)
    planted = home / "nested" / "cordis.patch.yml"
    planted.write_text("planted by a previous run")
    sibling = tmp_path / "engine-home" / "s2"
    sibling.mkdir()
    (sibling / "keep").write_text("keep")
    proc = _start(
        client,
        argv=["ls", "-A", str(home)],
        reset_dirs=[str(home), str(tmp_path / "engine-home" / "never-existed")],
        files=[{"path": str(home / "config.json"), "content_b64": _b64(b"{}")}],
    )
    out, _, _ = _drain(client, proc["proc_id"])
    assert out == b"config.json\n" and not planted.exists()
    assert (sibling / "keep").read_text() == "keep"


def test_cwd_may_be_created_by_files_after_reset(client, tmp_path):
    # 引擎主目录每次启动前被清空重建，它同时是 DSH 的启动目录：写文件后才存在，照样可以作 cwd。
    home = tmp_path / "engine-home" / "s1"
    (home / "stale").mkdir(parents=True)
    proc = _start(
        client,
        argv=["pwd"],
        cwd=str(home),
        reset_dirs=[str(home)],
        files=[{"path": str(home / "acp.patch.yml"), "content_b64": _b64(b"[]")}],
    )
    out, _, _ = _drain(client, proc["proc_id"])
    assert out == f"{home}\n".encode() and not (home / "stale").exists()


def test_cwd_that_files_do_not_create_is_still_rejected(client, tmp_path):
    missing = tmp_path / "nope"
    response = client.post(
        "/api/procs",
        json={
            "name": "x",
            "argv": ["true"],
            "cwd": str(missing),
            "files": [{"path": str(tmp_path / "elsewhere" / "f"), "content_b64": _b64(b"x")}],
        },
    )
    assert response.status_code == 400 and response.json()["detail"]["reason"] == "invalid_request"
    assert not (tmp_path / "elsewhere").exists()  # 校验在任何副作用之前


def test_reset_dirs_removes_a_planted_symlink_without_following_it(client, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "data").write_text("keep")
    link = tmp_path / "homes" / "s1"
    link.parent.mkdir()
    link.symlink_to(victim, target_is_directory=True)
    proc = _start(client, argv=["true"], reset_dirs=[str(link)])
    _drain(client, proc["proc_id"])
    assert not os.path.lexists(link) and (victim / "data").read_text() == "keep"


def _mountinfo(tmp_path, monkeypatch, *points: str) -> None:
    lines = [f"{20 + i} 1 0:{i} / {p.replace(' ', chr(92) + '040')} rw - ext4 /dev/x rw" for i, p in enumerate(points)]
    path = tmp_path / "mountinfo"
    path.write_text("\n".join(["1 0 0:1 / / rw - ext4 /dev/root rw", *lines]) + "\n")
    monkeypatch.setattr(procs_mod, "MOUNTINFO", path)


@pytest.mark.parametrize(
    "path",
    ["/", "/tmp", "/workspace", "relative/home", "/tmp/a/../b", "/tmp/a/", "//tmp/a", "/tmp/a/./b"],
)
def test_reset_dirs_rejects_root_top_level_and_unnormalized_paths(client, path):
    response = client.post("/api/procs", json={"name": "x", "argv": ["true"], "reset_dirs": [path]})
    assert response.status_code == 400 and response.json()["detail"]["reason"] == "invalid_request"


def test_reset_dirs_rejects_mount_points_and_dirs_containing_them(client, tmp_path, monkeypatch):
    work = tmp_path / "ws" / "work dir"
    role = tmp_path / "ws" / "role" / "notes"
    for directory in (work, role):
        directory.mkdir(parents=True)
        (directory / "user-file").write_text("user data")
    _mountinfo(tmp_path, monkeypatch, str(work), str(role))
    for path in (work, role, role.parent, tmp_path / "ws"):
        response = client.post("/api/procs", json={"name": "x", "argv": ["true"], "reset_dirs": [str(path)]})
        assert response.status_code == 400, path
        assert "挂载点" in response.json()["detail"]["message"]
    assert (work / "user-file").exists() and (role / "user-file").exists()
    _start(client, argv=["true"], reset_dirs=[str(tmp_path / "ws" / "engine-home")])   # 挂载点旁边的目录照常可清


def test_reset_dirs_rejects_a_symlinked_parent(client, tmp_path):
    real = tmp_path / "real"
    (real / "home").mkdir(parents=True)
    (real / "home" / "user-file").write_text("user data")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    response = client.post("/api/procs", json={"name": "x", "argv": ["true"], "reset_dirs": [str(alias / "home")]})
    assert response.status_code == 400 and response.json()["detail"]["reason"] == "invalid_request"
    assert (real / "home" / "user-file").exists()


def test_rejected_start_has_no_side_effects(client, tmp_path):
    ended = _start(client, name="engine", argv=["true"])
    _wait_ended(client, ended["proc_id"])
    home = tmp_path / "home"
    home.mkdir()
    (home / "keep").write_text("keep")
    body = {"name": "engine", "argv": ["true"], "reset_dirs": [str(home), "/tmp"]}
    assert client.post("/api/procs", json=body).status_code == 400
    assert (home / "keep").exists()   # 校验在任何删除之前
    assert client.get(f"/api/procs/{ended['proc_id']}").status_code == 200   # 同名的旧记录也还在


# ── 日志保留与长轮询 ───────────────────────────────────────────────────────────


def test_retention_keeps_the_latest_bytes_and_flags_truncation(client):
    block = bytes(range(256))
    script = (
        "import sys, time\n"
        "for _ in range(8):\n"
        "    sys.stdout.buffer.write(bytes(range(256))); sys.stdout.flush(); time.sleep(0.02)\n"
    )
    proc = _start(client, argv=[sys.executable, "-c", script], log_retain_bytes=100)
    info = _wait_ended(client, proc["proc_id"])
    expected = block * 8
    assert info["stdout_size"] == len(expected)
    body = _read(client, proc["proc_id"], out=0, wait=0)
    assert body["truncated"] is True and body["stdout_cursor"] == len(expected)
    assert base64.b64decode(body["stdout_b64"]) == expected[-100:]
    body = _read(client, proc["proc_id"], out=len(expected) - 40, wait=0)
    assert body["truncated"] is False and base64.b64decode(body["stdout_b64"]) == expected[-40:]
    segments = list((procs_mod.PROC_DIR / proc["proc_id"]).glob("stdout.*"))
    assert 1 <= len(segments) <= 2   # 更早的分段已删除


def test_long_poll_returns_early_on_new_data_and_on_exit(client):
    proc = _start(client, argv=["cat"])
    proc_id = proc["proc_id"]
    result = {}

    def poll(cursor):
        started = time.monotonic()
        result["body"] = _read(client, proc_id, out=cursor, wait=10)
        result["elapsed"] = time.monotonic() - started

    poller = threading.Thread(target=poll, args=(0,))
    poller.start()
    time.sleep(0.3)
    _write(client, proc_id, b"ping")
    poller.join(5)
    assert not poller.is_alive() and result["elapsed"] < 5
    assert base64.b64decode(result["body"]["stdout_b64"]) == b"ping"

    started = time.monotonic()
    body = _read(client, proc_id, out=4, wait=0.5)   # 没有新数据：等满 wait
    assert 0.4 <= time.monotonic() - started < 3 and body["stdout_b64"] == ""

    poller = threading.Thread(target=poll, args=(4,))
    poller.start()
    time.sleep(0.3)
    _write(client, proc_id, close=True)
    poller.join(5)
    assert not poller.is_alive() and result["elapsed"] < 5
    assert result["body"]["proc"]["state"] == "exited"


# ── 归属与活性 ─────────────────────────────────────────────────────────────────


def test_proc_outlives_the_request_and_the_client(manager):
    with TestClient(_app()) as first:
        proc = _start(first, argv=["cat"])
    # 第一个客户端连同它的事件循环都已结束：进程与输出泵照常
    with TestClient(_app()) as second:
        assert second.get(f"/api/procs/{proc['proc_id']}").json()["state"] == "running"
        _write(second, proc["proc_id"], b"still here")
        assert _read_stdout(second, proc["proc_id"], 10) == b"still here"

    async def cancelled_long_poll():
        task = asyncio.create_task(manager.read(proc["proc_id"], 10, 0, 10, 0))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancelled_long_poll())   # 读请求被取消（调用方断线）不影响进程
    assert manager.get(proc["proc_id"]).running


def _fake_process(root: Path, pid: int, environ: bytes) -> None:
    directory = root / str(pid)
    directory.mkdir()
    (directory / "stat").write_text("123 (x) " + " ".join(["S", *("0" for _ in range(18)), "100"]))
    (directory / "environ").write_bytes(b"UNRELATED=1\0" + environ)


def test_activity_counts_running_procs_without_touching_the_job_ledger(client):
    def activity():
        response = client.get("/api/terminal/activity")
        assert response.status_code == 200, response.text
        return response.json()

    body = activity()
    assert (body["running_jobs"], body["running_procs"], body["marked_processes"]) == (0, 0, 0)
    proc = _start(client, argv=["sleep", "30"])
    body = activity()
    assert body["running_jobs"] == 1 and body["running_procs"] == 1
    assert body["jobs"] == [] and body["unattributed_live"] == []
    # 带 proc 标记、脱离进程组的后代（主进程结束后仍在跑）也算活动
    _fake_process(bash_mod.PROC_ROOT, 4242, f"CR_SANDBOX_PROC_ID={proc['proc_id']}\0".encode())
    client.post(f"/api/procs/{proc['proc_id']}/signal", json={"signal": "KILL"})
    _wait_ended(client, proc["proc_id"])
    body = activity()
    assert body["running_jobs"] == 0 and body["running_procs"] == 0 and body["marked_processes"] == 1
    assert body["unattributed_live"] == []   # proc 标记不进任务来源台账
