import subprocess

import pytest

from app.tools import bash as bash_mod


@pytest.fixture(autouse=True)
def _job_dir(tmp_path, monkeypatch):
    """每个测试用独立的 job 目录与独立的 tmux socket，不碰 /tmp/cr-jobs 和默认 tmux server。"""
    sock = str(tmp_path / "tmux.sock")
    monkeypatch.setattr(bash_mod, "JOB_DIR", tmp_path / "cr-jobs")
    monkeypatch.setattr(bash_mod, "KILL_GRACE", 0.5)
    monkeypatch.setattr(bash_mod, "TMUX_SOCKET", sock)
    yield
    subprocess.run(["tmux", "-S", sock, "kill-server"], stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
