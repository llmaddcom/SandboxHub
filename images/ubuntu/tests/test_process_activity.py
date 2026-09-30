import errno
from pathlib import Path

import pytest

from app.tools import bash as module
from app.tools.base import ToolError


def stat(state="S", started=100):
    return "123 (name with ) spaces) " + " ".join([state, *("0" for _ in range(18)), str(started)])


def process(root, pid, *, state="S", marked=True):
    directory = root / str(pid)
    directory.mkdir()
    (directory / "stat").write_text(stat(state))
    marker = b"CR_SANDBOX_JOB_ID=j_01ARZ3NDEKTSV4RRFFQ69G5FAV\0" if marked else b""
    (directory / "environ").write_bytes(b"UNRELATED=value\0" + marker)
    return directory


def test_process_probe_ignores_zombies_and_does_not_require_job_records(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "PROC_ROOT", tmp_path)
    process(tmp_path, 1, marked=False)
    process(tmp_path, 2)
    process(tmp_path, 3, state="Z")
    assert module.count_marked_processes() == 1


def test_unreadable_process_is_unknown_instead_of_idle(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "PROC_ROOT", tmp_path)
    process(tmp_path, 1)
    monkeypatch.setattr(Path, "read_bytes", lambda _: (_ for _ in ()).throw(PermissionError(errno.EACCES, "denied")))
    with pytest.raises(ToolError, match="无法确认"):
        module.count_marked_processes()


def test_pid_reuse_during_probe_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "PROC_ROOT", tmp_path)
    process(tmp_path, 1)
    snapshots = iter([stat(started=100), stat(started=200)])
    monkeypatch.setattr(Path, "read_text", lambda _: next(snapshots))
    with pytest.raises(ToolError, match="无法确认"):
        module.count_marked_processes()


def test_process_disappearing_before_marker_read_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "PROC_ROOT", tmp_path)
    process(tmp_path, 1)
    monkeypatch.setattr(Path, "read_bytes", lambda _: (_ for _ in ()).throw(FileNotFoundError(errno.ENOENT, "exited")))
    with pytest.raises(ToolError, match="无法确认"):
        module.count_marked_processes()


def test_marked_parent_disappearing_during_probe_preserves_lease(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "PROC_ROOT", tmp_path)
    process(tmp_path, 1)
    reads = 0

    def read_stat(_):
        nonlocal reads
        reads += 1
        if reads > 1:
            raise FileNotFoundError(errno.ENOENT, "exited")
        return stat()

    monkeypatch.setattr(Path, "read_text", read_stat)
    assert module.count_marked_processes() == 1
