"""view 读到二进制文件时直接拒绝，返回固定前缀的错误，不返回带行号的乱码（SandboxHub#12）。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import file as file_router
from app.tools import ToolError
from app.tools.edit import BINARY_FILE_ERROR, EditTool


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("book.xlsx", b"PK\x03\x04\x14\x00\x06\x00" + b"x" * 64),
        ("legacy.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"x" * 64),
        ("paper.pdf", b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"),
        ("logo.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"),
        ("noext", b"ab\x00cd"),
    ],
)
async def test_view_rejects_binary(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    with pytest.raises(ToolError) as exc:
        await EditTool().view(str(path))
    assert exc.value.message.startswith(f"{BINARY_FILE_ERROR}: {path}")
    # 兼容只认解码报错特征的调用方
    assert "codec can't decode" in exc.value.message


@pytest.mark.asyncio
async def test_view_text_and_empty_files_unaffected(tmp_path):
    path = tmp_path / "a.csv"
    path.write_text("名称,数量\n苹果,3\n", encoding="utf-8")
    assert "苹果" in (await EditTool().view(str(path))).output
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    assert (await EditTool().view(str(empty))).total_lines == 0


def test_view_route_returns_400_with_binary_prefix(tmp_path):
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04" + b"\x00" * 32)
    app = FastAPI()
    app.include_router(file_router.router)
    with TestClient(app) as client:
        response = client.post("/api/file/view", json={"path": str(path)})
    assert response.status_code == 400
    assert response.json()["detail"] == f"查看文件失败: {BINARY_FILE_ERROR}: {path} 是二进制文件，不能按文本查看（codec can't decode）"
