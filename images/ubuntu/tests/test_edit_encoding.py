"""EditTool._read_file GB18030 编码回退的测试。"""

import pytest

from app.tools.edit import EditTool


@pytest.mark.asyncio
async def test_view_gb18030_file(tmp_path):
    # 模拟国内用户上传的 GBK/GB18030 编码 .sql 文件，UTF-8 严格解码会抛 UnicodeDecodeError
    path = tmp_path / "query.sql"
    path.write_bytes("-- 项目查询\nSELECT * FROM 合同表;\n".encode("gb18030"))

    tool = EditTool()
    result = await tool.view(str(path))

    assert "项目查询" in result.output
    assert "合同表" in result.output


@pytest.mark.asyncio
async def test_str_replace_gb18030_file(tmp_path):
    path = tmp_path / "query.sql"
    path.write_bytes("SELECT * FROM 合同表;\n".encode("gb18030"))

    tool = EditTool()
    result = await tool.str_replace(str(path), "合同表", "订单表")

    assert result.output is not None
    assert path.read_text(encoding="utf-8") == "SELECT * FROM 订单表;\n"


@pytest.mark.asyncio
async def test_view_utf8_file_unaffected(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("hello 世界\n", encoding="utf-8")

    tool = EditTool()
    result = await tool.view(str(path))

    assert "hello 世界" in result.output


@pytest.mark.asyncio
async def test_view_undecodable_bytes_falls_back_to_replace(tmp_path):
    # 既非 UTF-8 也非合法 GB18030 的任意二进制字节：不应抛异常，用 U+FFFD 兜底
    path = tmp_path / "bin.dat"
    path.write_bytes(b"\xff\xfe\x00\xff")

    tool = EditTool()
    result = await tool.view(str(path))

    assert result.output is not None
