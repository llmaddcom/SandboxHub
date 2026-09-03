import pytest

from app.tools import bash as bash_mod


@pytest.fixture(autouse=True)
def _job_dir(tmp_path, monkeypatch):
    """每个测试用独立的 job 目录，不碰 /tmp/cr-jobs。"""
    monkeypatch.setattr(bash_mod, "JOB_DIR", tmp_path / "cr-jobs")
    monkeypatch.setattr(bash_mod, "KILL_GRACE", 0.5)
    yield
