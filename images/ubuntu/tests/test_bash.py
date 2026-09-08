"""job 会话（app.tools.bash）——真实 tmux + bash 测试，覆盖 issue #30 验收项与 tmux 化新增项。"""

import asyncio
import os
import subprocess
import time

import pytest

from app.tools import bash as bash_mod
from app.tools.bash import (
    HEAD_BYTES,
    TAIL_BYTES,
    BashTool,
    JobSession,
    ToolError,
    _clean_tty,
    _head_tail_truncate,
    _new_job_id,
    session_name,
)


def _tmux(*args: str) -> str:
    return subprocess.run(
        ["tmux", "-S", bash_mod.TMUX_SOCKET, *args], capture_output=True, text=True, check=False
    ).stdout


# ── 纯函数 ───────────────────────────────────────────────────────────────────

def test_head_tail_short_text_unchanged():
    text = "hello world"
    assert _head_tail_truncate(text) == text


def test_head_tail_exact_limit_unchanged():
    text = "x" * (HEAD_BYTES + TAIL_BYTES)
    assert _head_tail_truncate(text) == text


def test_head_tail_truncates_middle():
    text = "A" * HEAD_BYTES + "M" * 10 + "Z" * TAIL_BYTES
    result = _head_tail_truncate(text)
    assert result.startswith("A" * HEAD_BYTES)
    assert result.endswith("Z" * TAIL_BYTES)
    assert "已省略" in result
    assert "M" not in result


def test_head_tail_reports_omitted_byte_count():
    extra = 100
    text = "x" * (HEAD_BYTES + TAIL_BYTES + extra)
    assert str(extra) in _head_tail_truncate(text)


def test_job_id_is_sortable_and_unique():
    a = _new_job_id()
    time.sleep(0.002)
    b = _new_job_id()
    assert a.startswith("j_") and len(a) == 28
    assert a < b
    assert _new_job_id() != _new_job_id()


def test_session_name_sanitized_for_tmux():
    assert session_name(None) == "default"
    assert session_name("") == "default"
    assert session_name("sess.abc:def/ghi") == "sess_abc_def_ghi"
    assert session_name("x" * 100) == "x" * 48
    assert session_name("Ab-1_2") == "Ab-1_2"


def test_clean_tty_normalizes_pty_output():
    assert _clean_tty("a\r\nb\r\n") == "a\nb\n"
    assert _clean_tty("\x1b[32mgreen\x1b[0m") == "green"
    assert _clean_tty("10%\r50%\r100%\n") == "100%\n"
    assert _clean_tty("\x1b]0;title\x07plain") == "plain"


# ── job 契约：submit / wait / kill ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_returns_running_then_wait_until_exited():
    """验收 1：长命令 wait 内未结束返回 running；反复 wait 直至 exited，输出含 done。"""
    tool = BashTool()
    job = await tool.submit("sleep 0.6; echo done", wait=0.1)
    assert job.status == "running"
    assert job.exit_code is None
    assert os.path.exists(job.log_path)

    job = await tool.wait(job.id, wait=0.1)
    assert job.status == "running"

    job = await tool.wait(job.id, wait=5)
    assert job.status == "exited"
    assert job.exit_code == 0
    output, cursor = job.read(0)
    assert output == "done\n"
    assert cursor == job.log_size()


@pytest.mark.asyncio
async def test_output_strips_script_markers_and_crlf():
    tool = BashTool()
    job = await tool.submit("printf 'a\\nb'", wait=5)
    assert job.status == "exited"
    assert job.read(0)[0] == "a\nb"
    raw = job.log_path.read_bytes()
    assert raw.startswith(b"Script started on") and b"Script done on" in raw


@pytest.mark.asyncio
async def test_wait_returns_incremental_output_from_cursor():
    tool = BashTool()
    job = await tool.submit("echo first; sleep 0.4; echo second", wait=0.15)
    first, cursor = job.read(0)
    assert first == "first\n"
    job = await tool.wait(job.id, wait=5)
    second, cursor2 = job.read(cursor)
    assert second == "second\n"
    assert cursor2 == job.log_size()
    # cursor 越界按日志末尾处理
    assert job.read(10_000) == ("", cursor2)


@pytest.mark.asyncio
async def test_exit_code_comes_from_pane_dead_status():
    tool = BashTool()
    job = await tool.submit("echo x; exit 7", wait=5)
    assert job.status == "exited" and job.exit_code == 7
    assert job.read(0)[0] == "x\n"


@pytest.mark.asyncio
async def test_multiple_jobs_run_in_parallel():
    """tmux 化：同容器多 job 并行，不再 409。"""
    tool = BashTool()
    a = await tool.submit("sleep 0.5; echo A", wait=0)
    b = await tool.submit("echo B", wait=5)
    assert a.status == "running" and b.status == "exited"
    assert b.read(0)[0] == "B\n"
    a = await tool.wait(a.id, wait=5)
    assert a.status == "exited" and a.read(0)[0] == "A\n"


@pytest.mark.asyncio
async def test_job_runs_in_tmux_window_named_by_job_id():
    tool = BashTool()
    job = await tool.submit("sleep 5", wait=0.2, session="conv-1")
    assert job.session == "conv-1"
    windows = _tmux("list-windows", "-t", "=conv-1", "-F", "#{window_name}").split()
    assert job.id in windows
    await tool.kill(job.id, "KILL")
    await tool.wait(job.id, 5)
    # 结束后窗口销毁，日志保留
    windows = _tmux("list-windows", "-t", "=conv-1", "-F", "#{window_name}").split()
    assert job.id not in windows
    assert os.path.exists(job.log_path)


@pytest.mark.asyncio
async def test_interactive_input_via_tmux_send_keys():
    """交互式：命令等 stdin 时用 tmux send-keys 喂输入。"""
    tool = BashTool()
    job = await tool.submit("read -r name; echo hello:$name", wait=0.3, session="conv-2")
    assert job.status == "running"
    _tmux("send-keys", "-t", f"=conv-2:{job.id}", "world", "Enter")
    job = await tool.wait(job.id, wait=5)
    assert job.status == "exited" and job.exit_code == 0
    assert job.read(0)[0].endswith("hello:world\n")


@pytest.mark.asyncio
async def test_window_closed_externally_settles_as_killed():
    tool = BashTool()
    job = await tool.submit("sleep 30", wait=0.2, session="conv-3")
    _tmux("kill-window", "-t", f"=conv-3:{job.id}")
    job = await tool.wait(job.id, wait=5)
    assert job.status == "killed"
    assert job.kill_reason == "window_closed"


@pytest.mark.asyncio
async def test_sessions_are_isolated(tmp_path):
    """按会话命名的 tmux session：cwd / env / 窗口互不可见。"""
    tool = BashTool()
    work = tmp_path / "w"
    work.mkdir()
    await tool.submit(f"cd {work} && export FOO=1", wait=5, session="s-a")
    a = await tool.submit("pwd; echo FOO=${FOO-unset}", wait=5, session="s-a")
    b = await tool.submit("pwd; echo FOO=${FOO-unset}", wait=5, session="s-b")
    assert a.read(0)[0] == f"{work.resolve()}\nFOO=1\n"
    assert b.read(0)[0].endswith("FOO=unset\n") and str(work) not in b.read(0)[0]
    # 各自的窗口只在各自的 tmux session 里（会话随最后一个窗口结束而消失，故用在跑的 job 看）
    ra = await tool.submit("sleep 30", wait=0.2, session="s-a")
    rb = await tool.submit("sleep 30", wait=0.2, session="s-b")
    assert _tmux("list-windows", "-t", "=s-a", "-F", "#{window_name}").split() == [ra.id]
    assert _tmux("list-windows", "-t", "=s-b", "-F", "#{window_name}").split() == [rb.id]
    for j in (ra, rb):
        await tool.kill(j.id, "KILL")
        await tool.wait(j.id, 5)


@pytest.mark.asyncio
async def test_cwd_and_exported_env_persist_across_jobs(tmp_path):
    """验收 2：cd / export 跨调用保留。"""
    tool = BashTool()
    work = tmp_path / "x"
    work.mkdir()
    await tool.submit(f"cd {work} && export FOO=1", wait=5)
    job = await tool.submit("pwd; echo FOO=$FOO", wait=5)
    assert job.read(0)[0] == f"{work.resolve()}\nFOO=1\n"

    # unset 也跨调用生效（环境按上一 job 的 env 全量重建）
    await tool.submit("unset FOO", wait=5)
    job = await tool.submit("echo FOO=${FOO-unset}", wait=5)
    assert job.read(0)[0] == "FOO=unset\n"


@pytest.mark.asyncio
async def test_env_values_with_newlines_survive():
    tool = BashTool()
    await tool.submit("export MULTI=$'l1\\nl2'", wait=5)
    job = await tool.submit('printf "%s" "$MULTI" | wc -l', wait=5)
    assert job.read(0)[0].strip() == "1"


@pytest.mark.asyncio
async def test_cwd_saved_even_when_command_exits_explicitly(tmp_path):
    tool = BashTool()
    job = await tool.submit(f"cd {tmp_path} && exit 7", wait=5)
    assert job.exit_code == 7
    job = await tool.submit("pwd", wait=5)
    assert job.read(0)[0].strip() == str(tmp_path.resolve())


@pytest.mark.asyncio
async def test_deleted_cwd_falls_back_with_note(tmp_path):
    tool = BashTool()
    gone = tmp_path / "gone"
    gone.mkdir()
    await tool.submit(f"cd {gone}", wait=5)
    gone.rmdir()
    job = await tool.submit("pwd", wait=5)
    assert job.exit_code == 0
    note = tool.pop_notes()
    assert note and "已不存在" in note


@pytest.mark.asyncio
async def test_kill_int_marks_killed_and_session_continues():
    """验收 3：kill 后 wait 立即返回 killed；会话仍可继续执行。"""
    tool = BashTool()
    job = await tool.submit("sleep 30", wait=0.3)
    job = await tool.kill(job.id, "INT")
    job = await tool.wait(job.id, wait=5)
    assert job.status == "killed"
    assert job.exit_code == 130
    assert job.kill_reason == "kill:INT"

    t0 = time.monotonic()
    again = await tool.wait(job.id, wait=30)
    assert again.status == "killed"
    assert time.monotonic() - t0 < 1

    nxt = await tool.submit("echo after", wait=5)
    assert nxt.status == "exited"
    assert nxt.read(0)[0] == "after\n"


@pytest.mark.asyncio
async def test_kill_KILL_on_int_ignoring_process():
    tool = BashTool()
    job = await tool.submit("trap '' INT; sleep 30", wait=0.3)
    await tool.kill(job.id, "INT")
    job = await tool.wait(job.id, wait=0.3)
    assert job.status == "running"
    job = await tool.kill(job.id, "KILL")
    job = await tool.wait(job.id, wait=5)
    assert job.status == "killed"
    assert job.exit_code == 137


@pytest.mark.asyncio
async def test_kill_finished_job_is_noop_and_bad_signal_rejected():
    tool = BashTool()
    job = await tool.submit("echo hi", wait=5)
    assert job.status == "exited"
    same = await tool.kill(job.id, "KILL")
    assert same.status == "exited" and same.kill_reason is None
    running = await tool.submit("sleep 5", wait=0)
    with pytest.raises(ToolError, match="不支持的信号"):
        await tool.kill(running.id, "HUP")
    await tool.kill(running.id, "KILL")
    await tool.wait(running.id, 5)


@pytest.mark.asyncio
async def test_unknown_job_id_raises():
    tool = BashTool()
    with pytest.raises(ToolError, match="未知 job"):
        await tool.wait("j_nope", wait=0)


@pytest.mark.asyncio
async def test_no_timeout_means_command_runs_to_completion():
    """验收 4（缩短版）：不传 timeout 的命令不被掐——超过旧默认/上限比例的时长仍 exited。"""
    tool = BashTool()
    job = await tool.submit("sleep 0.8; echo ok", wait=5)
    assert job.status == "exited"
    assert job.timeout is None
    assert job.read(0)[0] == "ok\n"


@pytest.mark.asyncio
async def test_timeout_kills_process_group_with_reason():
    tool = BashTool()
    job = await tool.submit("sleep 30", wait=5, timeout=0.3)
    assert job.status == "killed"
    assert job.kill_reason == "timeout"
    assert job.exit_code == 130


@pytest.mark.asyncio
async def test_timeout_escalates_to_sigkill_when_int_ignored():
    tool = BashTool()
    job = await tool.submit("trap '' INT; sleep 30", wait=5, timeout=0.3)
    assert job.status == "killed"
    assert job.kill_reason == "timeout"
    assert job.exit_code == 137


@pytest.mark.asyncio
async def test_timeout_kills_children_in_group():
    tool = BashTool()
    job = await tool.submit("sh -c 'sleep 30' & wait", wait=5, timeout=0.3)
    assert job.status == "killed"
    pgid = job.pid
    # 后台子进程默认忽略 SIGINT，包装 bash 先退；宽限后整个进程组被 SIGKILL 扫尾
    await asyncio.sleep(0.9)
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


@pytest.mark.asyncio
async def test_kill_int_sweeps_surviving_children():
    tool = BashTool()
    job = await tool.submit("sh -c 'sleep 30' & wait", wait=0.3)
    await tool.kill(job.id, "INT")
    job = await tool.wait(job.id, wait=5)
    assert job.status == "killed"
    pgid = job.pid
    await asyncio.sleep(0.9)
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


@pytest.mark.asyncio
async def test_normal_exit_leaves_nohup_background_children_alone():
    """正常退出不扫尾：nohup 的后台进程（如 dev server）活过 job 结束。
    （没有 nohup 的后台进程会随终端关闭收到 SIGHUP——这是 PTY 的正常语义，模型侧
    长驻进程应开 tmux 窗口或 nohup。）"""
    tool = BashTool()
    job = await tool.submit("nohup sh -c 'sleep 2' >/dev/null 2>&1 & echo started", wait=5)
    assert job.status == "exited"
    pgid = job.pid
    await asyncio.sleep(0.9)
    os.killpg(pgid, 0)  # 组仍存活
    os.killpg(pgid, 9)


@pytest.mark.asyncio
async def test_wait_is_clamped_to_max_wait(monkeypatch):
    monkeypatch.setattr(bash_mod, "MAX_WAIT", 0.2)
    tool = BashTool()
    t0 = time.monotonic()
    job = await tool.submit("sleep 5", wait=60)
    assert job.status == "running"
    assert time.monotonic() - t0 < 1.5
    await tool.kill(job.id, "KILL")


@pytest.mark.asyncio
async def test_large_output_is_head_tail_truncated_but_log_is_full():
    tool = BashTool()
    n = HEAD_BYTES + TAIL_BYTES + 1000
    job = await tool.submit(f"head -c {n} /dev/zero | tr '\\0' x", wait=10)
    output, cursor = job.read(0)
    assert cursor == job.log_size()
    assert "已省略" in output
    assert len(output.encode()) < n
    assert os.path.getsize(job.log_path) > n


@pytest.mark.asyncio
async def test_background_process_does_not_block_job():
    tool = BashTool()
    job = await tool.submit("(sleep 1 &) ; echo bg", wait=5)
    assert job.status == "exited"
    assert job.read(0)[0] == "bg\n"


@pytest.mark.asyncio
async def test_reaper_evicts_oldest_live_windows_but_protects_recent(monkeypatch):
    """活跃窗口达上限：淘汰最旧的，最近 PROTECT_RECENT 个不动（codex 64/8 的缩小版）。"""
    monkeypatch.setattr(bash_mod, "MAX_LIVE_WINDOWS", 3)
    monkeypatch.setattr(bash_mod, "PROTECT_RECENT", 1)
    tool = BashTool()
    old = await tool.submit("sleep 30", wait=0.2)
    await asyncio.sleep(1.1)  # window_activity 秒级，拉开先后
    mid = await tool.submit("sleep 30", wait=0.2)
    await asyncio.sleep(1.1)
    newest = await tool.submit("sleep 30", wait=0.2)
    fourth = await tool.submit("echo four", wait=5)
    assert fourth.status == "exited"
    old = await tool.wait(old.id, wait=5)
    assert old.status == "killed" and old.kill_reason == "evicted"
    assert not mid.finished and not newest.finished
    for j in (mid, newest):
        await tool.kill(j.id, "KILL")
        await tool.wait(j.id, 5)


@pytest.mark.asyncio
async def test_restart_kills_running_job_and_resets_state(tmp_path):
    tool = BashTool()
    initial = tool.session.state().cwd
    await tool.submit(f"cd {tmp_path} && export FOO=1", wait=5)
    running = await tool.submit("sleep 30", wait=0.3)
    result = await tool.restart()
    assert "重启" in (result.system or "")
    assert running.status == "killed"
    assert running.kill_reason == "restart"
    job = await tool.submit("pwd; echo FOO=${FOO-unset}", wait=5)
    assert job.read(0)[0] == f"{initial}\nFOO=unset\n"


@pytest.mark.asyncio
async def test_restart_only_touches_its_own_session(tmp_path):
    tool = BashTool()
    other = await tool.submit("sleep 30", wait=0.3, session="keep")
    await tool.restart(session="gone")
    assert not other.finished
    await tool.kill(other.id, "KILL")
    await tool.wait(other.id, 5)


@pytest.mark.asyncio
async def test_job_records_pruned_but_logs_kept(monkeypatch):
    monkeypatch.setattr(bash_mod, "MAX_JOB_RECORDS", 3)
    tool = BashTool()
    jobs = [await tool.submit(f"echo {i}", wait=5) for i in range(5)]
    assert len(tool.session.jobs) <= 3
    assert jobs[-1].id in tool.session.jobs
    assert all(os.path.exists(j.log_path) for j in jobs)


@pytest.mark.asyncio
async def test_session_env_skips_shell_and_tmux_managed_vars(tmp_path):
    session = JobSession(cwd=str(tmp_path), env={"PATH": os.environ["PATH"]})
    job = await session.start_job("export FOO=bar")
    await session.wait(job, 5)
    env = session.state().env
    assert env["FOO"] == "bar"
    assert env["PATH"] == os.environ["PATH"]
    assert not {"PWD", "OLDPWD", "SHLVL", "_", "TMUX", "TMUX_PANE"} & set(env)


# ── 旧契约：execute / execute_stream ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_legacy_execute_blocks_until_done_and_merges_stderr():
    tool = BashTool()
    job, result = await tool.execute_job("echo out; echo err >&2; exit 3")
    assert result.output == "out\nerr\n"
    assert result.error == ""
    assert job.exit_code == 3


@pytest.mark.asyncio
async def test_legacy_execute_timeout_returns_notice_not_exception():
    tool = BashTool()
    result = await tool.execute("sleep 30", timeout=0.3)
    assert "超时" in result.output
    assert "0.3" in result.output
    assert "session 继续可用" in result.output
    nxt = await tool.execute("echo alive")
    assert nxt.output == "alive\n"


@pytest.mark.asyncio
async def test_legacy_execute_caps_timeout_at_max(monkeypatch):
    monkeypatch.setattr(bash_mod, "MAX_TIMEOUT", 0.3)
    tool = BashTool()
    result = await tool.execute("sleep 30", timeout=9999)
    assert "0.3" in result.output


@pytest.mark.asyncio
async def test_legacy_execute_runs_alongside_running_job():
    """旧调用方并发提交：与在跑的 job 并行，不排队也不报错。"""
    tool = BashTool()
    running = await tool.submit("sleep 0.6; echo first", wait=0)
    result = await tool.execute("echo second")
    assert running.status == "running"
    assert result.output == "second\n"
    await tool.wait(running.id, 5)


@pytest.mark.asyncio
async def test_legacy_execute_preserves_cwd(tmp_path):
    tool = BashTool()
    await tool.execute(f"cd {tmp_path}")
    result = await tool.execute("pwd")
    assert result.output.strip() == str(tmp_path.resolve())


@pytest.mark.asyncio
async def test_legacy_execute_rejects_empty_command():
    tool = BashTool()
    with pytest.raises(ToolError, match="未提供命令"):
        await tool.execute("")
