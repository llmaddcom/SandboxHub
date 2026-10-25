"""v2（按工作区分配，ADR 0025）的纯逻辑：标识与路径校验、分配请求校验、注册表上限、
netguard 规则生成、自检汇总与能力判定。不碰 Docker。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import settings
from src.manager.container_manager import BridgeInfo
from src.manager.netguard import (
    PRIVATE_CIDRS,
    Capabilities,
    Capability,
    ImageFeatures,
    SelfTest,
    _is_loopback,
    build_rules,
    chain_names,
    probe_passes,
)
from src.manager.paths import split_relative, valid_id, valid_mount_name, valid_prefix
from src.manager.wa_instances import AcquireSpec, WorkAreaError, validate_spec
from src.manager.wa_registry import LimitExceeded, WorkAreaRegistry
from src.models import InputFileSpec, RoleMountSpec, WorkAreaInstance

SHA = "a" * 64


# ── 标识与路径 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["wa1", "WA_1-x", "a" * 64])
def test_valid_id_accepts(value):
    assert valid_id(value)


@pytest.mark.parametrize("value", ["", "-a", "_a", "a" * 65, "a/b", "a.b", "wa1\n", "..", None])
def test_valid_id_rejects(value):
    assert not valid_id(value)


def test_mount_name_rules():
    assert valid_mount_name("skills") and valid_mount_name("k2_b-c")
    for bad in ("Skills", "1abc", "", "a" * 33, "a/b", "skills\n"):
        assert not valid_mount_name(bad)


@pytest.mark.parametrize("path", ["/abs", "a//b", "a/./b", "a/../b", "..", "a\x00b", "a\nb", ""])
def test_split_relative_rejects(path):
    with pytest.raises(ValueError):
        split_relative(path)


def test_split_relative_ok():
    assert split_relative("a/b c/中文.txt") == ["a", "b c", "中文.txt"]


def test_valid_prefix():
    assert valid_prefix("roles/r1/skills/")
    for bad in ("", "/", "/roles/r1", "roles/../x", "roles//x"):
        assert not valid_prefix(bad), bad


def test_role_mount_key_is_stable_and_mode_sensitive():
    a = RoleMountSpec(name="skills", bucket="b", prefix="roles/r1/skills/", mode="ro")
    b = RoleMountSpec(name="other", bucket="b", prefix="/roles/r1/skills", mode="ro")
    c = RoleMountSpec(name="skills", bucket="b", prefix="roles/r1/skills/", mode="rw")
    assert a.key == b.key  # 名字与首尾斜杠不影响共享
    assert a.key != c.key  # ro / rw 各自一个 rclone 进程
    assert a.target == "/workspace/role/skills"


# ── 分配请求校验 ─────────────────────────────────────────────────────────────


def _spec(**kw) -> AcquireSpec:
    base = dict(work_area_id="wa1", user_id="u1", role_id="r1")
    base.update(kw)
    return AcquireSpec(**base)


def test_validate_spec_ok():
    validate_spec(
        _spec(
            mounts=[
                RoleMountSpec("skills", "b", "roles/r1/skills/", "ro"),
                RoleMountSpec("notes", "b", "roles/r1/notes/", "rw"),
            ],
            inputs=[InputFileSpec("a/x.txt", "b", "userspace/u1/blobs/aa/x", SHA, 3)],
        )
    )


@pytest.mark.parametrize(
    "mounts",
    [
        [RoleMountSpec("skills", "b", "roles/r2/skills/", "ro")],  # 别的数字人
        [RoleMountSpec("skills", "b", "roles/r1", "ro")],  # 整个角色根
        [RoleMountSpec("skills", "b", "roles/r10/x", "ro")],  # 前缀相似
        [RoleMountSpec("skills", "b", "roles/r1/../r2/x", "ro")],
        [RoleMountSpec("skills", "", "roles/r1/x", "ro")],
        [RoleMountSpec("Skills", "b", "roles/r1/x", "ro")],
        [RoleMountSpec("a", "b", "roles/r1/x", "ro"), RoleMountSpec("a", "b", "roles/r1/y", "ro")],
    ],
)
def test_validate_spec_rejects_mounts(mounts):
    with pytest.raises(WorkAreaError) as ei:
        validate_spec(_spec(mounts=mounts))
    assert ei.value.status == 400 and ei.value.reason == "invalid_mount"


@pytest.mark.parametrize(
    "inputs",
    [
        [InputFileSpec("../x", "b", "k", SHA, 1)],
        [InputFileSpec("x", "b", "k", "z" * 64, 1)],
        [InputFileSpec("x", "b", "k", SHA, 1), InputFileSpec("x", "b", "k2", SHA, 1)],
        [InputFileSpec("x", "b", "k", SHA, 1), InputFileSpec("x/y", "b", "k2", SHA, 1)],
        [InputFileSpec("x", "", "k", SHA, 1)],
    ],
)
def test_validate_spec_rejects_inputs(inputs):
    with pytest.raises(WorkAreaError) as ei:
        validate_spec(_spec(inputs=inputs))
    assert ei.value.reason == "invalid_inputs"


def test_validate_spec_rejects_bad_ids():
    with pytest.raises(WorkAreaError):
        validate_spec(_spec(work_area_id="../x"))
    with pytest.raises(WorkAreaError):
        validate_spec(_spec(user_id="u1\n"))


# ── 注册表与上限 ─────────────────────────────────────────────────────────────


def _inst(sid: str, wa: str, user: str = "u1", role: str = "r1") -> WorkAreaInstance:
    return WorkAreaInstance(
        sandbox_id=sid, work_area_id=wa, user_id=user, role_id=role, sandbox_type="code", api_token="t"
    )


async def test_registry_limits_per_role_and_user():
    reg = WorkAreaRegistry()
    await reg.reserve(_inst("s1", "w1"), max_per_role=2, max_per_user=3)
    await reg.reserve(_inst("s2", "w2"), max_per_role=2, max_per_user=3)
    with pytest.raises(LimitExceeded) as ei:
        await reg.reserve(_inst("s3", "w3"), max_per_role=2, max_per_user=3)
    assert ei.value.scope == "role" and ei.value.limit == 2
    assert {h.sandbox_id for h in ei.value.holders} == {"s1", "s2"}
    # 同用户换数字人：数字人名额够，用户名额 3 用完后才拒
    await reg.reserve(_inst("s4", "w4", role="r2"), max_per_role=2, max_per_user=3)
    with pytest.raises(LimitExceeded) as ei:
        await reg.reserve(_inst("s5", "w5", role="r3"), max_per_role=2, max_per_user=3)
    assert ei.value.scope == "user" and ei.value.limit == 3
    # 别的用户使用同一个数字人：名额按 (用户, 数字人) 计，不受影响
    await reg.reserve(_inst("s6", "w6", user="u2"), max_per_role=2, max_per_user=3)
    await reg.reserve(_inst("s7", "w7", user="u2"), max_per_role=2, max_per_user=3)


async def test_registry_one_instance_per_work_area_and_release_frees_slot():
    reg = WorkAreaRegistry()
    await reg.reserve(_inst("s1", "w1"), max_per_role=1, max_per_user=1)
    with pytest.raises(ValueError):
        await reg.reserve(_inst("s2", "w1"), max_per_role=5, max_per_user=5)
    await reg.remove("s1")
    await reg.reserve(_inst("s3", "w2"), max_per_role=1, max_per_user=1)
    assert reg.find_by_work_area("w2").sandbox_id == "s3"
    assert reg.find_by_work_area("w1") is None


async def test_registry_zero_limit_means_unlimited():
    reg = WorkAreaRegistry()
    for i in range(5):
        await reg.reserve(_inst(f"s{i}", f"w{i}"), max_per_role=0, max_per_user=0)
    assert len(reg.list_all()) == 5


# ── netguard 规则 ────────────────────────────────────────────────────────────


def _bridges() -> list[BridgeInfo]:
    return [
        BridgeInfo("n-wa", "br-aaa", "172.30.0.1", "172.30.0.2", False),
        BridgeInfo("n-iso-wa", "br-bbb", "172.31.0.1", "172.31.0.2", True),
    ]


def test_chain_names_short_and_scoped(monkeypatch):
    fwd, inp = chain_names()
    assert len(fwd) <= 28 and len(inp) <= 28
    assert fwd.endswith("-F") and inp.endswith("-I")
    monkeypatch.setattr(settings, "CONTAINER_LABEL", "sandboxhub.other")
    assert chain_names() != (fwd, inp)  # 同机多套 Hub 各管各的链


def test_build_rules_covers_every_bridge():
    rules = build_rules(_bridges(), allow_ports=[8118], allow_cidrs=["10.9.0.0/16"])
    fwd, inp = chain_names()
    lines = rules.splitlines()
    assert lines[0] == "*filter" and lines[-1] == "COMMIT"
    assert f":{fwd} - [0:0]" in lines and f"-F {inp}" in lines
    for br, gw in (("br-aaa", "172.30.0.2/32"), ("br-bbb", "172.31.0.2/32")):
        assert f"-A {fwd} -i {br} -o {br} -s {gw} -j ACCEPT" in lines
        assert f"-A {fwd} -i {br} -m conntrack --ctstate DNAT -j DROP" in lines
        for cidr in PRIVATE_CIDRS:
            assert f"-A {fwd} -i {br} ! -o {br} -d {cidr} -j DROP" in lines
        # 白名单 CIDR 在私网 DROP 之前
        allow = lines.index(f"-A {fwd} -i {br} ! -o {br} -d 10.9.0.0/16 -j RETURN")
        drop = lines.index(f"-A {fwd} -i {br} ! -o {br} -d 10.0.0.0/8 -j DROP")
        assert allow < drop
        # INPUT：回包放行在最前，兜底 DROP 在最后
        est = lines.index(f"-A {inp} -i {br} -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN")
        port = lines.index(f"-A {inp} -i {br} -p tcp --dport 8118 -j RETURN")
        final = lines.index(f"-A {inp} -i {br} -j DROP")
        assert est < port < final


def test_build_rules_without_bridges_only_resets_chains():
    rules = build_rules([], allow_ports=[], allow_cidrs=[])
    assert "-A " not in rules and "COMMIT" in rules


@pytest.mark.parametrize(
    "result,expect,ok",
    [
        ("timeout", "filtered", True),
        ("unreachable", "filtered", True),
        ("refused", "filtered", False),  # 有回 RST = 包到了宿主，没被丢
        ("open", "filtered", False),
        ("refused", "closed", True),
        ("open", "closed", False),
        ("open", "open", True),
        ("timeout", "open", False),
    ],
)
def test_probe_passes(result, expect, ok):
    assert probe_passes(result, expect) is ok


def test_summarize_reports_failures_per_item():
    results = {
        "n-wa": [
            {"kind": "peer", "host": "172.30.0.9", "port": 18080, "expect": "filtered", "result": "timeout"},
            {"kind": "host", "host": "172.30.0.1", "port": 5432, "expect": "filtered", "result": "refused"},
            {"kind": "storage", "host": "cr-host", "port": 9000, "expect": "closed", "result": "refused"},
            {"kind": "gateway", "host": "cr-host", "port": 8118, "expect": "open", "result": "open"},
        ]
    }
    items = SelfTest.summarize(results)
    assert items["isolation_peer"].ok
    assert not items["isolation_host"].ok and "5432" in items["isolation_host"].detail
    assert items["isolation_storage"].ok and items["gateway_reachable"].ok


def test_summarize_without_gateway_forward_skips_positive_control():
    items = SelfTest.summarize({"n": [{"kind": "peer", "host": "x", "port": 1, "expect": "filtered", "result": "timeout"}]})
    assert items["gateway_reachable"].ok


def test_capabilities_pending_until_first_selftest():
    caps = Capabilities()
    assert caps.failing() == ["selftest_pending"]
    assert caps.to_dict()["ok"] is False


def test_capabilities_require_every_item_and_image():
    items = {name: Capability(True) for name in Capabilities.REQUIRED}
    items["image_code"] = Capability(True)
    caps = Capabilities(items=items, checked_at=datetime.now(tz=timezone.utc))
    assert caps.ok_for("code")
    assert caps.failing("ubuntu") == ["image_ubuntu"]  # 缺项即不满足
    items["isolation_storage"] = Capability(False, "x")
    assert "isolation_storage" in caps.failing("code")


def test_image_features_from_labels():
    assert ImageFeatures.from_labels(None) == ImageFeatures()   # 镜像不存在
    assert ImageFeatures.from_labels({"sandboxhub.api_auth": "1"}) == ImageFeatures()   # 旧镜像缺标签
    features = ImageFeatures.from_labels(
        {"sandboxhub.procs": "1", "sandboxhub.engines": '{"pi":"1.4.0","dsh":"0.2.1-rc.3"}'}
    )
    assert features.procs is True and features.engines == {"pi": "1.4.0", "dsh": "0.2.1-rc.3"}
    assert ImageFeatures.from_labels({"sandboxhub.procs": "0"}).procs is False
    for bad in ("not json", "[]", '{"pi": 1}', '{"": "1.0"}', '{"pi": ""}'):
        assert ImageFeatures.from_labels({"sandboxhub.procs": "1", "sandboxhub.engines": bad}).engines == {}


def test_capabilities_report_procs_and_engines_per_type():
    pending = Capabilities().to_dict()
    assert pending["procs"] == {t: False for t in settings.sandbox_types}
    assert pending["engines"] == {t: {} for t in settings.sandbox_types}
    caps = Capabilities(images={"code": ImageFeatures(True, {"pi": "1.4.0"})}).to_dict()
    assert caps["procs"] == {"ubuntu": False, "code": True}
    assert caps["engines"] == {"ubuntu": {}, "code": {"pi": "1.4.0"}}


async def test_selftest_reads_image_features_from_configured_images():
    labels = {
        settings.image_v2_for_type("code"): {
            "sandboxhub.api_auth": "1",
            "sandboxhub.procs": "1",
            "sandboxhub.engines": json.dumps({"pi": "1.4.0", "dsh": "0.2.1-rc.3"}),
        },
        settings.image_v2_for_type("ubuntu"): {"sandboxhub.api_auth": "1"},
    }
    manager = MagicMock()
    manager.image_labels = AsyncMock(side_effect=lambda image: labels.get(image))
    sidecar = MagicMock()
    sidecar.name = "cr-test-fs"
    sidecar.running = AsyncMock(return_value=False)   # 只跑静态项与镜像项
    result = await SelfTest(sidecar, MagicMock(), manager).run()
    data = result.to_dict()
    assert data["procs"] == {"ubuntu": False, "code": True}
    assert data["engines"] == {"ubuntu": {}, "code": {"pi": "1.4.0", "dsh": "0.2.1-rc.3"}}
    assert data["items"]["image_code"]["ok"] and data["items"]["image_ubuntu"]["ok"]


@pytest.mark.parametrize("host,loop", [("127.0.0.1", True), ("localhost", True), ("[::1]", True), ("minio", False), ("10.0.0.5", False)])
def test_is_loopback(host, loop):
    assert _is_loopback(host) is loop


def test_selftest_targets_skip_loopback_minio(monkeypatch):
    monkeypatch.setattr(settings, "MINIO_ENDPOINT", "127.0.0.1:9000")
    st = SelfTest.__new__(SelfTest)
    targets = st._targets(_bridges()[0], "172.30.0.9")
    storage = [t for t in targets if t["kind"] == "storage"]
    assert all(t["host"] != "127.0.0.1" for t in storage)
    # 宿主 IP 上的 MinIO 端口仍在 host 目标里
    assert any(t["kind"] == "host" and t["port"] == 9000 and t["host"] == "172.30.0.1" for t in targets)


# ── 配置 ────────────────────────────────────────────────────────────────────


def test_wa_names_derive_from_v1_names(monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_NETWORK", "cr-sb-net")
    monkeypatch.setattr(settings, "SANDBOX_NETWORK_ISOLATED", "cr-sb-isolated")
    monkeypatch.setattr(settings, "SANDBOX_GATEWAY_NAME", "cr-host")
    assert settings.wa_network == "cr-sb-net-wa"
    assert settings.wa_network_isolated == "cr-sb-isolated-wa"
    assert settings.wa_network_for_mode("deny") == "cr-sb-isolated-wa"
    assert settings.wa_network_for_mode("allow") == "cr-sb-net-wa"
    assert settings.wa_gateway_name == "cr-host-wa" and settings.fs_sidecar_name == "cr-host-fs"


@pytest.mark.parametrize("host,loop", [("127.0.0.1", True), ("localhost", True), ("0.0.0.0", False), ("192.168.1.101", False)])
def test_hub_bound_to_loopback(monkeypatch, host, loop):
    monkeypatch.setattr(settings, "SANDBOX_HUB_HOST", host)
    assert settings.hub_bound_to_loopback is loop


def test_gateway_forwards_minio_toggle(monkeypatch):
    monkeypatch.setattr(settings, "MINIO_ENDPOINT", "127.0.0.1:9000")
    with_minio = settings.gateway_forwards(include_minio=True)
    without = settings.gateway_forwards(include_minio=False)
    assert any(port == 9000 for port, _ in with_minio)
    assert all(port != 9000 for port, _ in without)
