"""api/file/replace 多级容错匹配 + 结构化错误响应的测试。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.tools.edit import EditTool
from app.tools.matcher import (
    MatchError,
    apply_spans,
    find_replacement_spans,
)


# ── matcher 纯函数：策略链 ────────────────────────────────────────────────────

def test_exact_match():
    content = "alpha\nbeta\ngamma\n"
    spans, strategy = find_replacement_spans(content, "beta", replace_all=False)
    assert strategy == "exact"
    assert apply_spans(content, spans, "BETA") == "alpha\nBETA\ngamma\n"


def test_trailing_whitespace_tolerated():
    # 文件行带行尾空白，old_str 没有 —— 精确匹配会失败，rstrip 容错命中
    content = "def f():   \n    return 1\n"
    old = "def f():\n    return 1"
    spans, strategy = find_replacement_spans(content, old)
    assert strategy == "rstrip_line"
    assert apply_spans(content, spans, "def g():\n    return 2") == "def g():\n    return 2\n"


def test_indentation_difference_tolerated():
    # 文件 4 空格缩进，old_str 2 空格 —— strip 容错命中
    content = "if x:\n        do_thing()\n        done()\n"
    old = "if x:\n  do_thing()\n  done()"
    spans, strategy = find_replacement_spans(content, old)
    assert strategy == "strip_line"


def test_fancy_quotes_and_dashes_normalized():
    # 文件含花引号与 em-dash，old_str 用 ASCII —— Unicode 归一化命中
    content = 'msg = “hello” — world\n'
    old = 'msg = "hello" - world'
    spans, strategy = find_replacement_spans(content, old)
    assert strategy == "unicode_normalized"
    out = apply_spans(content, spans, "msg = ok")
    assert out == "msg = ok\n"


def test_nbsp_normalized():
    content = "a = 1\n"
    old = "a = 1"
    spans, strategy = find_replacement_spans(content, old)
    assert strategy == "unicode_normalized"


def test_block_anchor_middle_line_drift():
    # 首尾行锚定一致，中间行有细微差异 —— 块锚定命中
    content = "def run():\n    x = compute_value(a, b)\n    return x\n"
    old = "def run():\n    x = compute(a, b)\n    return x"
    spans, strategy = find_replacement_spans(content, old)
    assert strategy == "block_anchor"


# ── 保险一：唯一性强制 ───────────────────────────────────────────────────────

def test_not_unique_rejected():
    content = "foo\nfoo\nfoo\n"
    with pytest.raises(MatchError) as ei:
        find_replacement_spans(content, "foo", replace_all=False)
    assert ei.value.reason == "not_unique"
    assert ei.value.info["occurrences"] == 3
    assert ei.value.info["lines"] == [1, 2, 3]


def test_replace_all_applies_every_occurrence():
    content = "foo\nfoo\nfoo\n"
    spans, _ = find_replacement_spans(content, "foo", replace_all=True)
    assert len(spans) == 3
    assert apply_spans(content, spans, "bar") == "bar\nbar\nbar\n"


def test_not_found_lists_tried_strategies():
    content = "alpha\nbeta\n"
    with pytest.raises(MatchError) as ei:
        find_replacement_spans(content, "nonexistent", replace_all=False)
    assert ei.value.reason == "not_found"
    assert "exact" in ei.value.info["tried"]
    assert "block_anchor" in ei.value.info["tried"]


def test_empty_old_str_rejected():
    with pytest.raises(MatchError) as ei:
        find_replacement_spans("abc", "", replace_all=False)
    assert ei.value.reason == "not_found"


# ── 保险二：跨度保险 ─────────────────────────────────────────────────────────

def test_disproportionate_block_match_rejected():
    # 首尾锚定行相同，但中间被塞入大量无关内容，块大小超出容差 → 不应被块锚定吞掉
    filler = "\n".join(f"    noise_line_{i}()" for i in range(40))
    content = f"def start():\n{filler}\n    end()\n"
    old = "def start():\n    a()\n    end()"
    with pytest.raises(MatchError) as ei:
        find_replacement_spans(content, old, replace_all=False)
    # 块大小超 ±25% 容差，块锚定不命中，最终回 not_found（而非误吞 40 行）
    assert ei.value.reason == "not_found"


# ── 端到端：结构化错误响应 ───────────────────────────────────────────────────

@pytest.fixture
def client():
    from app.routers import file as file_router

    file_router.edit_tool = EditTool()  # 重置全局工具实例
    app = FastAPI()
    app.include_router(file_router.router)
    return TestClient(app)


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


def test_endpoint_success_with_whitespace_tolerance(client, tmp_path):
    path = _write(tmp_path, "a.py", "def f():   \n    return 1\n")
    resp = client.post(
        "/api/file/replace",
        json={"path": path, "old_str": "def f():\n    return 1", "new_str": "def g():\n    return 2"},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_endpoint_not_unique_structured_400(client, tmp_path):
    path = _write(tmp_path, "b.txt", "foo\nfoo\n")
    resp = client.post(
        "/api/file/replace",
        json={"path": path, "old_str": "foo", "new_str": "bar"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["reason"] == "not_unique"
    assert body["occurrences"] == 2
    assert "detail" in body
    # 文件未被随机改动
    assert (tmp_path / "b.txt").read_text() == "foo\nfoo\n"


def test_endpoint_not_found_structured_400(client, tmp_path):
    path = _write(tmp_path, "c.txt", "hello\n")
    resp = client.post(
        "/api/file/replace",
        json={"path": path, "old_str": "missing", "new_str": "x"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["reason"] == "not_found"
    assert "tried" in body


def test_endpoint_replace_all(client, tmp_path):
    path = _write(tmp_path, "d.txt", "x\nx\nx\n")
    resp = client.post(
        "/api/file/replace",
        json={"path": path, "old_str": "x", "new_str": "y", "replace_all": True},
    )
    assert resp.status_code == 200
    assert (tmp_path / "d.txt").read_text() == "y\ny\ny\n"


def test_endpoint_missing_file_path_error(client, tmp_path):
    resp = client.post(
        "/api/file/replace",
        json={"path": str(tmp_path / "nope.txt"), "old_str": "a", "new_str": "b"},
    )
    assert resp.status_code == 400
    assert resp.json()["reason"] == "path_error"


def test_endpoint_delete_via_empty_new_str(client, tmp_path):
    path = _write(tmp_path, "e.txt", "keep\nremove\n")
    resp = client.post(
        "/api/file/replace",
        json={"path": path, "old_str": "remove\n", "new_str": None},
    )
    assert resp.status_code == 200
    assert (tmp_path / "e.txt").read_text() == "keep\n"
