"""cr-fs 内的 fsops.py：在临时目录上直接调用（CRFS_ROOT 指向 tmp），验证不跟随租户软链接、
不被 FIFO 卡住、tar 导入只写普通文件与目录且不穿过软链接。"""
from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from src.config import REPO_ROOT

_spec = importlib.util.spec_from_file_location("crfs_fsops", REPO_ROOT / "images" / "fs" / "fsops.py")
fsops = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fsops)


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(fsops, "CRFS_ROOT", str(tmp_path))
    (tmp_path / "staging").mkdir()
    for wa in ("wa1", "wa2"):
        fsops.make_work_area(wa)
    (tmp_path / "work-areas" / "wa2" / "work" / "secret.txt").write_text("other tenant")
    return tmp_path


def _wa(root: Path, wa: str = "wa1", sub: str = "work") -> Path:
    return root / "work-areas" / wa / sub


def _tar(root: Path, entries: list[tuple[str, bytes | None]], *, extra=None) -> str:
    """entries: (name, data)；data=None 表示目录。extra(tf) 可追加特殊条目。"""
    path = root / "staging" / "in.tar"
    with tarfile.open(path, "w") as tf:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)
            else:
                info.size = len(data)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(data))
        if extra:
            extra(tf)
    return str(path)


def test_ids_reject_trailing_newline(root):
    with pytest.raises(fsops.OpError) as ei:
        fsops.work_area_dir("wa1\n")
    assert ei.value.code == fsops.EXIT_INVALID


def test_usage_and_list_skip_symlinks(root):
    work = _wa(root)
    (work / "a.txt").write_text("hello")
    (work / "d").mkdir()
    (work / "d" / "b.bin").write_bytes(b"x" * 10)
    (work / "link-to-other").symlink_to(_wa(root, "wa2"))
    (work / "file-link").symlink_to(_wa(root, "wa2") / "secret.txt")
    os.mkfifo(work / "pipe")

    usage = fsops.usage("wa1")
    assert usage == {"exists": True, "bytes": 15, "files": 2}

    listing = fsops.list_files("wa1", "work", 100)
    assert [f["path"] for f in listing["files"]] == ["a.txt", "d/b.bin"]
    assert listing["skipped"] == 2  # file-link 与 pipe；指向目录的软链接不进入也不计数
    assert "secret.txt" not in json.dumps(listing)


def test_list_truncates(root):
    for i in range(5):
        (_wa(root) / f"f{i}").write_text("x")
    listing = fsops.list_files("wa1", "work", 3)
    assert listing["truncated"] is True and len(listing["files"]) == 3


def test_read_refuses_symlink_and_symlinked_parent(root):
    work = _wa(root)
    (work / "file-link").symlink_to(_wa(root, "wa2") / "secret.txt")
    (work / "dir-link").symlink_to(_wa(root, "wa2"))
    out, err = io.BytesIO(), io.StringIO()
    with pytest.raises(fsops.OpError) as ei:
        fsops.read_file("wa1", "work", "file-link", out, err)
    assert ei.value.code == fsops.EXIT_NOT_REGULAR
    with pytest.raises(fsops.OpError) as ei:
        fsops.read_file("wa1", "work", "dir-link/work/secret.txt", out, err)
    assert ei.value.code == fsops.EXIT_NOT_REGULAR
    assert out.getvalue() == b""


def test_read_refuses_fifo_without_blocking(root):
    os.mkfifo(_wa(root) / "pipe")
    with pytest.raises(fsops.OpError) as ei:
        fsops.read_file("wa1", "work", "pipe", io.BytesIO(), io.StringIO())
    assert ei.value.code == fsops.EXIT_NOT_REGULAR


def test_read_reports_before_after_stat(root):
    (_wa(root, sub="deliverables") / "r.md").write_text("report")
    out, err = io.BytesIO(), io.StringIO()
    fsops.read_file("wa1", "deliverables", "r.md", out, err)
    before, after = (json.loads(line) for line in err.getvalue().splitlines())
    assert out.getvalue() == b"report" and before == after and before["size"] == 6


def test_checksum_batch_reports_content_and_missing_files(root):
    (_wa(root) / "a.txt").write_bytes(b"hello")
    result = fsops.checksums("wa1", "work", ["a.txt", "nope.txt"])
    assert result["schema_version"] == 1
    found, missing = result["items"]
    assert found["sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert found["size"] == 5 and found["stable"] is True
    assert missing == {"path": "nope.txt", "found": False}


@pytest.mark.parametrize("kind", ["file_link", "parent_link", "fifo", "escape"])
def test_checksum_batch_refuses_non_regular_or_escaping_paths(root, kind):
    work = _wa(root)
    if kind == "file_link":
        (work / "bad").symlink_to(_wa(root, "wa2") / "secret.txt")
        path = "bad"
    elif kind == "parent_link":
        (work / "bad").symlink_to(_wa(root, "wa2"))
        path = "bad/secret.txt"
    elif kind == "fifo":
        os.mkfifo(work / "bad")
        path = "bad"
    else:
        path = "../wa2/work/secret.txt"
    with pytest.raises(fsops.OpError) as error:
        fsops.checksums("wa1", "work", [path])
    assert error.value.code == (fsops.EXIT_INVALID if kind == "escape" else fsops.EXIT_NOT_REGULAR)


def test_checksum_batch_bounds_request_and_reads_only_staging_manifest(root):
    with pytest.raises(fsops.OpError):
        fsops.checksums("wa1", "work", ["a"] * 129)
    name = "checksums-" + "a" * 32 + ".json"
    (root / "staging" / name).write_text(json.dumps(["missing"]))
    assert fsops.checksum_manifest("wa1", "work", name)["items"] == [{"path": "missing", "found": False}]
    with pytest.raises(fsops.OpError):
        fsops.checksum_manifest("wa1", "work", "../work-areas/wa2/work/secret.txt")


def test_read_missing_and_invalid(root):
    with pytest.raises(fsops.OpError) as ei:
        fsops.read_file("wa1", "work", "nope.txt", io.BytesIO(), io.StringIO())
    assert ei.value.code == fsops.EXIT_NOT_FOUND
    with pytest.raises(fsops.OpError) as ei:
        fsops.read_file("wa1", "work", "../../wa2/work/secret.txt", io.BytesIO(), io.StringIO())
    assert ei.value.code == fsops.EXIT_INVALID
    with pytest.raises(fsops.OpError) as ei:
        fsops.read_file("wa1", "tmp", "x", io.BytesIO(), io.StringIO())
    assert ei.value.code == fsops.EXIT_INVALID


def test_import_writes_files_and_dirs(root):
    tar = _tar(root, [("./", None), ("./src", None), ("./src/main.py", b"print(1)\n"), ("README.md", b"# hi")])
    result = fsops.import_tar("wa1", "work", tar)
    assert result == {"files": 2, "bytes": 13}
    assert (_wa(root) / "src" / "main.py").read_text() == "print(1)\n"
    assert (_wa(root) / "README.md").read_text() == "# hi"


def test_import_overwrites_regular_file(root):
    (_wa(root) / "a.txt").write_text("old content that is longer")
    fsops.import_tar("wa1", "work", _tar(root, [("a.txt", b"new")]))
    assert (_wa(root) / "a.txt").read_text() == "new"


def test_import_does_not_follow_existing_symlinks(root):
    work = _wa(root)
    (work / "escape").symlink_to(_wa(root, "wa2"))
    (work / "victim").symlink_to(_wa(root, "wa2") / "secret.txt")
    for entries in ([("escape/work/secret.txt", b"pwned")], [("victim", b"pwned")]):
        with pytest.raises(fsops.OpError) as ei:
            fsops.import_tar("wa1", "work", _tar(root, entries))
        assert ei.value.code == fsops.EXIT_NOT_REGULAR
    assert (_wa(root, "wa2") / "secret.txt").read_text() == "other tenant"


def test_import_rejects_links_devices_and_escapes(root):
    def add_symlink(tf):
        info = tarfile.TarInfo("evil")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc"
        tf.addfile(info)

    with pytest.raises(fsops.OpError) as ei:
        fsops.import_tar("wa1", "work", _tar(root, [], extra=add_symlink))
    assert ei.value.code == fsops.EXIT_INVALID
    for name in ("../wa2/work/x", "/abs", "a/../../x"):
        with pytest.raises(fsops.OpError):
            fsops.import_tar("wa1", "work", _tar(root, [(name, b"x")]))
    assert not (_wa(root, "wa2") / "x").exists()


def test_import_requires_staging(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "x.tar"
    with tarfile.open(outside, "w"):
        pass
    with pytest.raises(fsops.OpError) as ei:
        fsops.import_tar("wa1", "work", str(outside))
    assert ei.value.code == fsops.EXIT_INVALID


def test_remove_work_area_does_not_follow_symlinks(root):
    (_wa(root) / "escape").symlink_to(_wa(root, "wa2"))
    assert fsops.remove_work_area("wa1") == {"removed": True}
    assert not (root / "work-areas" / "wa1").exists()
    assert (_wa(root, "wa2") / "secret.txt").exists()
    assert fsops.remove_work_area("wa1") == {"removed": False}


def test_main_exit_codes(root, capsys):
    assert fsops.main(["fsops", "usage", "wa1"]) == 0
    assert json.loads(capsys.readouterr().out)["exists"] is True
    assert fsops.main(["fsops", "usage", "../x"]) == fsops.EXIT_INVALID
    assert fsops.main(["fsops", "stat", "wa1", "work", "missing"]) == fsops.EXIT_NOT_FOUND
    assert fsops.main(["fsops", "bogus"]) == fsops.EXIT_INVALID


def test_mount_status_ignores_unrelated_mounts(root):
    assert fsops.mount_status() == {"mounts": {}}
