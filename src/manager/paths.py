"""v2 路径与标识校验：工作区 ID、实例内相对路径。与 images/fs/fsops.py 口径一致。"""
from __future__ import annotations

import re

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_MOUNT_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")


def valid_id(value: str) -> bool:
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


def require_id(value: str, what: str) -> str:
    if not valid_id(value):
        raise ValueError(f"非法{what}：{value!r}")
    return value


def valid_mount_name(value: str) -> bool:
    return isinstance(value, str) and bool(_MOUNT_NAME_RE.fullmatch(value))


def split_relative(path: str) -> list[str]:
    """把相对路径拆成段；拒绝绝对路径、空段、``.``/``..`` 与控制字符。"""
    if not path or path.startswith("/") or "\x00" in path or len(path) > 1024:
        raise ValueError(f"非法相对路径：{path!r}")
    parts = path.split("/")
    for part in parts:
        if part in ("", ".", "..") or any(ord(ch) < 0x20 for ch in part) or len(part) > 255:
            raise ValueError(f"非法相对路径：{path!r}")
    return parts


def valid_prefix(prefix: str) -> bool:
    """角色目录的 MinIO 前缀：非空、无 ``..``、不以 / 开头（不允许挂整个桶）。"""
    stripped = (prefix or "").strip("/")
    if not stripped or prefix.startswith("/"):
        return False
    try:
        split_relative(stripped)
    except ValueError:
        return False
    return True
