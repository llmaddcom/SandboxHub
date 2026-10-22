#!/usr/bin/env python3
"""cr-fs 内的工作区文件操作：SandboxHub 经 ``docker exec`` 以 root 调用。

工作区目录由租户容器以 root 写入，Hub 进程（普通用户）可能无权读取或删除，
因此读取、统计、导入、删除都在 cr-fs 内完成。

所有路径限定在 ``$CRFS_ROOT/work-areas/<work_area_id>/<root>/`` 之下；逐级以
``O_NOFOLLOW`` 相对目录 fd 打开（遍历用 ``os.fwalk``），不跟随租户写入的软链接（软链接可能
指向其他工作区）。租户实例可能同时在写，先检查后操作的路径字符串会被换成软链接，故一律不用。

命令：usage / list / stat / read / mkwa / rmwa / import / mounts（见 ``main``）。stdout 输出 JSON
（``read`` 输出文件内容，读前读后的 size/mtime 写 stderr）。

退出码：0 成功；2 参数非法；3 不存在；4 不是普通文件或路径含软链接。
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import threading

CRFS_ROOT = os.environ.get("CRFS_ROOT", "/srv")
WORK_AREA_ROOTS = ("work", "deliverables")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_CHUNK = 1024 * 1024
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

EXIT_INVALID = 2
EXIT_NOT_FOUND = 3
EXIT_NOT_REGULAR = 4


class OpError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def work_area_dir(work_area_id: str) -> str:
    if not _ID_RE.fullmatch(work_area_id or ""):
        raise OpError(EXIT_INVALID, f"非法工作区 ID：{work_area_id!r}")
    return os.path.join(CRFS_ROOT, "work-areas", work_area_id)


def _root_dir(work_area_id: str, root: str) -> str:
    if root not in WORK_AREA_ROOTS:
        raise OpError(EXIT_INVALID, f"非法目录：{root!r}")
    return os.path.join(work_area_dir(work_area_id), root)


def split_relative(path: str) -> list[str]:
    """把相对路径拆成段；拒绝绝对路径、空段、``.``/``..`` 与控制字符。"""
    if not path or path.startswith("/") or "\x00" in path:
        raise OpError(EXIT_INVALID, f"非法相对路径：{path!r}")
    parts = path.split("/")
    for part in parts:
        if part in ("", ".", "..") or any(ord(ch) < 0x20 for ch in part):
            raise OpError(EXIT_INVALID, f"非法相对路径：{path!r}")
    return parts


def _open_parent(base: str, parts: list[str], *, create: bool = False) -> int:
    """逐级以 O_NOFOLLOW 打开 ``parts[:-1]``，返回父目录 fd；``create`` 时缺的目录就地创建。"""
    try:
        fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except FileNotFoundError as exc:
        raise OpError(EXIT_NOT_FOUND, f"目录不存在：{base}") from exc
    try:
        for part in parts[:-1]:
            fd = _enter(fd, part, create=create)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _enter(fd: int, part: str, *, create: bool) -> int:
    """从目录 fd 进入子目录 ``part``（不跟随软链接），关闭旧 fd、返回新 fd。"""
    if create:
        try:
            os.mkdir(part, 0o755, dir_fd=fd)
        except FileExistsError:
            pass
    try:
        nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
    except FileNotFoundError as exc:
        raise OpError(EXIT_NOT_FOUND, "路径不存在") from exc
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise OpError(EXIT_NOT_REGULAR, "路径含软链接或非目录") from exc
        raise
    os.close(fd)
    return nfd


def open_regular(work_area_id: str, root: str, relpath: str) -> int:
    parts = split_relative(relpath)
    parent = _open_parent(_root_dir(work_area_id, root), parts)
    try:
        try:
            # O_NONBLOCK：租户放的 FIFO 不会把读取卡住（随后按非普通文件拒绝）
            fd = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent
            )
        except FileNotFoundError as exc:
            raise OpError(EXIT_NOT_FOUND, "文件不存在") from exc
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise OpError(EXIT_NOT_REGULAR, "目标是软链接") from exc
            raise
    finally:
        os.close(parent)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OpError(EXIT_NOT_REGULAR, "目标不是普通文件")
    return fd


def _walk_files(base: str):
    """逐个产出 ``(相对路径, stat)``：只含普通文件；其余类型产出 ``(相对路径, None)``。

    ``os.fwalk(follow_symlinks=False)`` 相对目录 fd 打开子目录并校验 inode，租户中途把目录
    换成软链接也走不出 ``base``。目录与文件按名字排序，结果稳定。
    """
    for dirpath, dirnames, filenames, dirfd in os.fwalk(base, follow_symlinks=False):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, base)
        for name in sorted(filenames):
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            try:
                st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
            except OSError:
                continue
            yield rel, (st if stat.S_ISREG(st.st_mode) else None)


def usage(work_area_id: str) -> dict:
    """工作区实际占用（普通文件的表观大小之和，不跟随软链接）。"""
    total = files = 0
    base = work_area_dir(work_area_id)
    if not os.path.isdir(base):
        return {"exists": False, "bytes": 0, "files": 0}
    for _, st in _walk_files(base):
        if st is not None:
            total += st.st_size
            files += 1
    return {"exists": True, "bytes": total, "files": files}


def list_files(work_area_id: str, root: str, max_files: int) -> dict:
    """列出目录下的普通文件（相对路径、大小、修改时间）；软链接与特殊文件跳过。"""
    base = _root_dir(work_area_id, root)
    out: list[dict] = []
    skipped = 0
    truncated = False
    if not os.path.isdir(base):
        return {"exists": False, "files": [], "truncated": False, "skipped": 0}
    for rel, st in _walk_files(base):
        if st is None:
            skipped += 1
            continue
        if len(out) >= max_files:
            truncated = True
            break
        out.append({"path": rel, "size": st.st_size, "mtime_ns": st.st_mtime_ns})
    return {"exists": True, "files": out, "truncated": truncated, "skipped": skipped}


def stat_file(work_area_id: str, root: str, relpath: str) -> dict:
    fd = open_regular(work_area_id, root, relpath)
    try:
        st = os.fstat(fd)
        return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    finally:
        os.close(fd)


def checksums(work_area_id: str, root: str, paths: list[str]) -> dict:
    """有界读取普通文件指纹；不存在逐项返回，软链接和非法路径明确失败。"""
    _root_dir(work_area_id, root)
    if not isinstance(paths, list) or not 1 <= len(paths) <= 128:
        raise OpError(EXIT_INVALID, "指纹路径数量须为 1 到 128")
    for path in paths:
        if not isinstance(path, str) or len(path) > 1024:
            raise OpError(EXIT_INVALID, "指纹路径格式无效")
        split_relative(path)
    items = []
    for path in paths:
        try:
            fd = open_regular(work_area_id, root, path)
        except OpError as error:
            if error.code != EXIT_NOT_FOUND:
                raise
            items.append({"path": path, "found": False})
            continue
        try:
            before = os.fstat(fd)
            digest = hashlib.sha256()
            size = 0
            while block := os.read(fd, _CHUNK):
                digest.update(block)
                size += len(block)
            after = os.fstat(fd)
            items.append({
                "path": path, "found": True, "size": size,
                "sha256": digest.hexdigest(), "mtime_ns": after.st_mtime_ns,
                "stable": size == before.st_size == after.st_size
                and before.st_mtime_ns == after.st_mtime_ns,
            })
        finally:
            os.close(fd)
    return {"schema_version": 1, "items": items}


def checksum_manifest(work_area_id: str, root: str, name: str) -> dict:
    """仅读取 Hub 在宿主暂存区生成的指纹请求，避免超长 exec 参数。"""
    if not re.fullmatch(r"checksums-[0-9a-f]{32}\.json", name):
        raise OpError(EXIT_INVALID, "指纹清单名称无效")
    parent = os.open(os.path.join(CRFS_ROOT, "staging"), _DIR_FLAGS)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise OpError(EXIT_INVALID, "指纹清单超过上限或不是普通文件")
        paths = json.load(stream)
    return checksums(work_area_id, root, paths)


def read_file(work_area_id: str, root: str, relpath: str, out, err) -> None:
    """内容写 ``out``；读前、读后的 (size, mtime_ns) 各一行 JSON 写 ``err``，供调用方判稳定。"""
    fd = open_regular(work_area_id, root, relpath)
    try:
        before = os.fstat(fd)
        err.write(json.dumps({"size": before.st_size, "mtime_ns": before.st_mtime_ns}) + "\n")
        while True:
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                break
            out.write(chunk)
        after = os.fstat(fd)
        err.write(json.dumps({"size": after.st_size, "mtime_ns": after.st_mtime_ns}) + "\n")
    finally:
        os.close(fd)


def make_work_area(work_area_id: str) -> dict:
    base = work_area_dir(work_area_id)
    for root in WORK_AREA_ROOTS:
        os.makedirs(os.path.join(base, root), mode=0o755, exist_ok=True)
    return {"path": base}


def remove_work_area(work_area_id: str) -> dict:
    base = work_area_dir(work_area_id)
    if not os.path.lexists(base):
        return {"removed": False}
    shutil.rmtree(base)
    return {"removed": True}


def _write_member(dest: str, parts: list[str], member: tarfile.TarInfo, source) -> None:
    parent = _open_parent(dest, parts, create=True)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            fd = os.open(parts[-1], flags, 0o644, dir_fd=parent)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO):
                raise OpError(EXIT_NOT_REGULAR, f"目标已存在且不是普通文件：{member.name!r}") from exc
            raise
    finally:
        os.close(parent)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OpError(EXIT_NOT_REGULAR, f"目标已存在且不是普通文件：{member.name!r}")
        os.ftruncate(fd, 0)
        with os.fdopen(fd, "wb", closefd=False) as out:
            shutil.copyfileobj(source, out, _CHUNK)
        os.fchmod(fd, 0o755 if member.mode & 0o111 else 0o644)
        os.utime(fd, (member.mtime, member.mtime))
    finally:
        os.close(fd)


def import_tar(work_area_id: str, root: str, tar_path: str) -> dict:
    """把 tar 解到 ``<root>/``：只接受普通文件与目录；逐级 O_NOFOLLOW 打开，不穿过软链接。"""
    staging = os.path.join(CRFS_ROOT, "staging") + os.sep
    if not os.path.realpath(tar_path).startswith(staging):
        raise OpError(EXIT_INVALID, f"tar 须位于 {staging}")
    dest = _root_dir(work_area_id, root)
    os.makedirs(dest, mode=0o755, exist_ok=True)
    count = 0
    total = 0
    with tarfile.open(tar_path, mode="r:*") as archive:
        members = archive.getmembers()
        planned: list[tuple[tarfile.TarInfo, list[str]]] = []
        for member in members:
            if not (member.isfile() or member.isdir()):
                raise OpError(EXIT_INVALID, f"不支持的条目类型：{member.name!r}")
            name = member.name.rstrip("/")
            while name.startswith("./"):
                name = name[2:]
            if name in ("", "."):
                continue
            planned.append((member, split_relative(name)))
        for member, parts in planned:
            if member.isdir():
                os.close(_open_parent(dest, [*parts, ""], create=True))
                continue
            source = archive.extractfile(member)
            if source is None:
                raise OpError(EXIT_INVALID, f"条目无法读取：{member.name!r}")
            with source:
                _write_member(dest, parts, member, source)
            count += 1
            total += member.size
    return {"files": count, "bytes": total}


def _unescape_mountinfo(raw: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), raw)


def _stat_alive(point: str, timeout: float) -> bool:
    """stat 挂载点：失败（rclone 已退出）或超时（FUSE 卡死）都算失效。"""
    result: list[bool] = []

    def probe() -> None:
        try:
            os.stat(point)
            result.append(True)
        except OSError:
            result.append(False)

    worker = threading.Thread(target=probe, daemon=True)
    worker.start()
    worker.join(timeout)
    return bool(result and result[0])


def mount_status(timeout: float = 3.0) -> dict:
    """``$CRFS_ROOT/mnt/<key>`` 上的挂载及其存活。"""
    prefix = os.path.join(CRFS_ROOT, "mnt") + "/"
    mounts: dict[str, dict] = {}
    with open("/proc/self/mountinfo", encoding="utf-8") as fh:
        for line in fh:
            fields = line.split()
            if "-" not in fields:
                continue
            point = _unescape_mountinfo(fields[4])
            key = point[len(prefix):]
            if not point.startswith(prefix) or not key or "/" in key:
                continue
            fstype = fields[fields.index("-") + 1]
            mounts[key] = {"fstype": fstype, "alive": _stat_alive(point, timeout)}
    return {"mounts": mounts}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return EXIT_INVALID
    cmd, args = argv[1], argv[2:]
    try:
        if cmd == "usage" and len(args) == 1:
            result = usage(args[0])
        elif cmd == "list" and len(args) in (2, 3):
            result = list_files(args[0], args[1], int(args[2]) if len(args) == 3 else 5000)
        elif cmd == "stat" and len(args) == 3:
            result = stat_file(*args)
        elif cmd == "checksums" and len(args) == 3:
            result = checksum_manifest(*args)
        elif cmd == "read" and len(args) == 3:
            read_file(*args, sys.stdout.buffer, sys.stderr)
            sys.stdout.buffer.flush()
            sys.stderr.flush()
            return 0
        elif cmd == "mkwa" and len(args) == 1:
            result = make_work_area(args[0])
        elif cmd == "rmwa" and len(args) == 1:
            result = remove_work_area(args[0])
        elif cmd == "import" and len(args) == 3:
            result = import_tar(*args)
        elif cmd == "mounts" and not args:
            json.dump(mount_status(), sys.stdout)
            sys.stdout.write("\n")
            sys.stdout.flush()
            # 卡在 FUSE 上的探测线程不等，直接退出
            os._exit(0)
        else:
            print(f"未知命令或参数个数不对：{cmd} {args}", file=sys.stderr)
            return EXIT_INVALID
    except OpError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
    except (ValueError, tarfile.TarError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_INVALID
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
