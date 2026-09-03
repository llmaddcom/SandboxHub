"""配置「三个家」对账（issue #28）。

守护：config/system.yaml 恰好声明全部系统键（不缺不多）；系统键只认 system.yaml
（env 同名变量退役并告警）；缺键 / 坏值仅该项回退代码默认；.env.example 只留部署项。
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import yaml
from loguru import logger

from src.config import (
    REPO_ROOT,
    RETIRED_ENV_KEYS,
    SYSTEM_FIELDS,
    SYSTEM_KNOBS,
    SYSTEM_YAML_PATH,
    Settings,
    load_settings,
)

_ENV_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=")


def _yaml_keys(path: Path) -> set[str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        f"{section}.{name}"
        for section, body in data.items()
        if isinstance(body, dict)
        for name in body
    }


@contextmanager
def _warnings() -> Iterator[list[str]]:
    records: list[str] = []
    sink_id = logger.add(lambda m: records.append(m.record["message"]), level="WARNING")
    try:
        yield records
    finally:
        logger.remove(sink_id)


def test_system_yaml_declares_exactly_all_system_keys():
    """system.yaml 是系统键唯一可写位置：缺 = 无家可归，多 = 死键。"""
    declared = {k.key for k in SYSTEM_KNOBS}
    in_file = _yaml_keys(SYSTEM_YAML_PATH)
    assert in_file == declared, f"system.yaml 缺: {declared - in_file}; 多: {in_file - declared}"


def test_knobs_map_to_settings_fields_uniquely():
    fields = set(Settings.model_fields)
    knob_fields = [k.field for k in SYSTEM_KNOBS]
    assert len(knob_fields) == len(set(knob_fields)), "声明表 field 重复"
    keys = [k.key for k in SYSTEM_KNOBS]
    assert len(keys) == len(set(keys)), "声明表 key 重复"
    assert SYSTEM_FIELDS <= fields, SYSTEM_FIELDS - fields
    for knob in SYSTEM_KNOBS:
        assert knob.key.count(".") == 1 and knob.description, knob


def test_env_example_only_declares_deploy_keys():
    """.env.example 只留连接 / 密钥 / 本机 / 网络项，系统键不得回流。"""
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    env_keys = {m.group(1) for line in text.splitlines() if (m := _ENV_LINE.match(line))}
    deploy_fields = set(Settings.model_fields) - SYSTEM_FIELDS
    assert env_keys == deploy_fields, (
        f".env.example 缺: {deploy_fields - env_keys}; 多（系统键/死键）: {env_keys - deploy_fields}"
    )


def test_checked_in_system_yaml_loads_without_warnings():
    with _warnings() as records:
        s = load_settings(env_file=None)
    assert not [r for r in records if "system.yaml" in r], records
    assert s.image_for_type("ubuntu") == s.DOCKER_IMAGE_UBUNTU


def test_system_keys_read_from_yaml_only(tmp_path, monkeypatch):
    """env 同名变量无效并告警；文件值生效；坏值 / 缺键仅该项回退默认；null = 沿用默认。"""
    path = tmp_path / "system.yaml"
    path.write_text(
        "warm_pool:\n  ubuntu: 7\n  code: bad\n"
        "proxy:\n  read_timeout: 12\n"
        "workspace:\n  mount_enabled: null\n  rclone_vfs_write_back: 5s\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WARM_POOL_UBUNTU", "99")
    monkeypatch.setenv("SANDBOX_HUB_PORT", "9999")
    with _warnings() as records:
        s = load_settings(system_yaml=path, env_file=None)

    assert s.WARM_POOL_UBUNTU == 7            # 文件值生效，env 99 被忽略
    assert s.WARM_POOL_CODE == 0              # 坏值回退默认
    assert s.PROXY_READ_TIMEOUT == 12.0       # int → float 收敛
    assert s.WORKSPACE_MOUNT_ENABLED is True  # null = 默认
    assert s.RCLONE_VFS_WRITE_BACK == "5s"
    assert s.RECONCILE_INTERVAL == 60         # 缺键回退默认
    assert s.SANDBOX_HUB_PORT == 9999         # 部署项照常读 env

    joined = "\n".join(records)
    assert "已退役" in joined and "WARM_POOL_UBUNTU" in joined
    assert "warm_pool.code" in joined and "非法" in joined
    assert "缺键 reconcile.interval" in joined


def test_missing_yaml_falls_back_to_defaults(tmp_path, monkeypatch):
    for k in RETIRED_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    with _warnings() as records:
        s = load_settings(system_yaml=tmp_path / "missing.yaml", env_file=None)
    assert s.WARM_POOL_UBUNTU == 3 and s.DOCKER_IMAGE_CODE == "sandbox-code:latest"
    assert any("不存在" in r for r in records)


def test_dotenv_retired_keys_ignored(tmp_path, monkeypatch):
    """.env 里残留的旧旋钮同样退役：告警并忽略，不影响部署项读取。"""
    for k in RETIRED_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("WARM_POOL_UBUNTU=42\nSANDBOX_NETWORK=custom\n", encoding="utf-8")
    with _warnings() as records:
        s = load_settings(system_yaml=tmp_path / "missing.yaml", env_file=env)
    assert s.WARM_POOL_UBUNTU == 3 and s.SANDBOX_NETWORK == "custom"
    assert any("已退役" in r and "WARM_POOL_UBUNTU" in r for r in records)


def test_explicit_overrides_win():
    s = load_settings(env_file=None, WARM_POOL_UBUNTU=5, MINIO_ENDPOINT="m:9000")
    assert s.WARM_POOL_UBUNTU == 5 and s.MINIO_ENDPOINT == "m:9000"
