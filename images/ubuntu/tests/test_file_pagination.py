"""分页只读取有限片段；超长行与 UTF-8/GB18030 续读不能跳过内容。"""
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import file as file_router
from app.tools.edit import EditTool


def page_text(output: str) -> str:
    body = output.split("\n", 1)[1].rsplit("\n[", 1)[0]
    return re.sub(r"(?m)^ *\d+\t(?:\[column=\d+\] )?", "", body)


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["utf-8", "gb18030"])
async def test_all_pages_reconstruct_file_without_skips(tmp_path, encoding):
    original = "头部\n" + "中" * 25000 + "\n" + "短行\n" * 700 + "尾部"
    path = tmp_path / "large.txt"
    path.write_bytes(original.encode(encoding))
    tool = EditTool()
    offset, column = 1, 0
    rendered = []
    for _ in range(500):
        result = await tool.view(str(path), [offset, offset + 49], max_chars=512, column=column)
        rendered.append(page_text(result.output))
        assert len(result.output) <= 512 + 300
        assert result.path == str(path)
        if result.next_offset is None:
            assert result.total_lines == 703
            break
        assert (result.next_offset, result.next_column) > (offset, column)
        assert result.truncated
        offset, column = result.next_offset, result.next_column
    else:
        pytest.fail("pagination did not finish")
    assert "".join(rendered) == original


@pytest.mark.asyncio
async def test_view_does_not_use_whole_file_reads(tmp_path, monkeypatch):
    path = tmp_path / "large.txt"
    path.write_text("hello\n" * 10000)
    monkeypatch.setattr(Path, "read_bytes", lambda *_: pytest.fail("whole file read"))
    result = await EditTool().view(str(path), [1, 500], max_chars=256)
    assert result.next_offset < 500
    assert result.total_lines is None


@pytest.mark.asyncio
async def test_read_past_requested_end_and_empty_file(tmp_path):
    path = tmp_path / "small.txt"
    path.write_text("first\nsecond")
    result = await EditTool().view(str(path), [1, 500])
    assert result.next_offset is None and result.total_lines == 2
    path.write_text("")
    result = await EditTool().view(str(path), [1, 500])
    assert result.next_offset is None and result.total_lines == 0


def test_view_route_exposes_continuation_and_keeps_long_line(tmp_path):
    path = tmp_path / "line.txt"
    path.write_text("x" * 900)
    app = FastAPI()
    app.include_router(file_router.router)
    with TestClient(app) as client:
        response = client.post("/api/file/view", json={"path": str(path), "max_chars": 256})
        assert response.status_code == 200
        data = response.json()
        assert data["next_offset"] == 1 and data["next_column"] > 0
        assert data["truncated"] is True and data["path"] == str(path)
