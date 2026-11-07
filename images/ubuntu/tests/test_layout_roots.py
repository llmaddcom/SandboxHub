"""布局 v2（SandboxHub#71）：只读位置写入给出明确原因，平台保留目录拒绝写入。

Hub 按布局注入 SANDBOX_READONLY_ROOTS / SANDBOX_HIDDEN_ROOTS / SANDBOX_CWD；未注入（v1）时保持原有行为。
"""

import errno
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import file as file_router
from app.tools.base import ToolError, filesystem_error_detail, hidden_root_error
from app.tools.edit import EditTool

V2_ENV = {
    "SANDBOX_CWD": "/workspace",
    "SANDBOX_READONLY_ROOTS": "/mnt/drive:/opt/cr:/self/diary:/workspace/AGENTS.md:/workspace/CLAUDE.md",
}


@pytest.fixture
def v2_env(monkeypatch, tmp_path):
    for key, value in V2_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SANDBOX_HIDDEN_ROOTS", str(tmp_path / "self" / ".cr"))
    return tmp_path


def test_erofs_names_readonly_roots_and_copy_target(v2_env):
    detail = filesystem_error_detail("/mnt/drive/a.md", OSError(errno.EROFS, "Read-only file system"))
    assert "只读位置" in detail and "/mnt/drive、/opt/cr" in detail and "复制到 /workspace 下" in detail


def test_erofs_without_layout_env_still_explains(monkeypatch):
    for key in ("SANDBOX_CWD", "SANDBOX_READONLY_ROOTS"):
        monkeypatch.delenv(key, raising=False)
    detail = filesystem_error_detail("/x", OSError(errno.EROFS, "Read-only file system"))
    assert "只读位置" in detail and "可写目录" in detail


def test_other_errors_unchanged(monkeypatch):
    assert filesystem_error_detail("/x", OSError(errno.EACCES, "denied")) == "/x: [Errno 13] denied"


def test_hidden_root_matches_only_inside(v2_env):
    hidden = v2_env / "self" / ".cr"
    assert hidden_root_error(hidden / "versions" / "a") is not None
    assert hidden_root_error(hidden) is not None
    assert hidden_root_error(v2_env / "self" / ".crx") is None
    assert hidden_root_error(v2_env / "self" / "about.md") is None


@pytest.mark.asyncio
async def test_edit_write_refuses_hidden_root_without_touching_disk(v2_env):
    target = v2_env / "self" / ".cr" / "versions" / "a.md"
    with pytest.raises(ToolError) as error:
        await EditTool().write(str(target), "x")
    assert "平台保留目录" in error.value.message
    assert not (v2_env / "self").exists()  # 连父目录都没建


@pytest.mark.asyncio
async def test_edit_write_readonly_parent_reports_reason(v2_env, monkeypatch):
    def fail(self, *args, **kwargs):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(Path, "mkdir", fail)
    with pytest.raises(ToolError) as error:
        await EditTool().write("/mnt/drive/new/a.md", "x")
    assert "只读位置" in error.value.message


@pytest.mark.asyncio
async def test_edit_write_readonly_file_reports_reason(v2_env, monkeypatch):
    def fail(self, *args, **kwargs):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(Path, "write_text", fail)
    with pytest.raises(ToolError) as error:
        await EditTool().write(str(v2_env / "a.md"), "x")
    assert "只读位置" in error.value.message and "/workspace" in error.value.message


def _client():
    app = FastAPI()
    app.include_router(file_router.router)
    return TestClient(app)


def test_upload_into_hidden_root_is_400(v2_env):
    with _client() as client:
        response = client.post(
            "/api/file/upload",
            data={"dest_path": str(v2_env / "self" / ".cr" / "x")},
            files={"file": ("x", b"content")},
        )
    assert response.status_code == 400 and "平台保留目录" in response.json()["detail"]


def test_upload_into_readonly_root_is_403_with_reason(v2_env, monkeypatch):
    def fail(*args):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(file_router, "_save_upload", fail)
    with _client() as client:
        response = client.post(
            "/api/file/upload", data={"dest_path": str(v2_env / "x")}, files={"file": ("x", b"content")}
        )
    assert response.status_code == 403 and "只读位置" in response.json()["detail"]


def test_write_endpoint_passes_reason_through(v2_env):
    with _client() as client:
        response = client.post(
            "/api/file/write", json={"path": str(v2_env / "self" / ".cr" / "a"), "file_text": "x"}
        )
    assert response.status_code == 400 and "平台保留目录" in response.json()["detail"]
