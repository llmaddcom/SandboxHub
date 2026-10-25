"""
netguard：v2 网络隔离规则与自检（ADR 0025）

宿主上 Postgres / Redis / MinIO / Hub / API 等端口多监听 0.0.0.0，能联网的沙盒经网桥网关
就能直连。v2 网络在 Docker 层关闭 ICC（实例互不可达），再由 cr-fs 代下发两条自有链：

- ``CRSB-<h>-F``（DOCKER-USER 第 1 条跳入）：网关进出放行；网关自身流量照常；拦截经 DNAT
  的连接（Docker 发布到宿主的端口，如 MinIO 9000）；拦截发往私网 / CGNAT / 链路本地的流量
  （白名单 CIDR 除外）。
- ``CRSB-<h>-I``（INPUT 第 1 条跳入）：已建立连接的回包放行（Hub → 实例 API 依赖它）；
  网关放行；白名单端口放行；其余从 v2 网桥到宿主的新连接一律丢弃。

规则只匹配 v2 网桥接口，v1 网络行为不变（回滚依赖 v1）。每次下发用 iptables-restore -n
原子替换两条链的内容，跳转缺失则补上；Docker 重启、网关重建后由对账周期自动恢复。

自检（``SelfTest``）用探针容器实测：实例之间、实例到宿主端口、实例到 MinIO 都必须不可达，
实例到网关转发端口必须可达（阳性对照，防「网络全断」被误判为隔离成功）。
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import docker
import docker.errors
from loguru import logger

from src.config import _endpoint_port, settings
from src.manager.container_manager import (
    API_AUTH_LABEL,
    ENGINES_LABEL,
    PROCS_LABEL,
    BridgeInfo,
    ContainerManager,
)
from src.manager.fs_sidecar import FsSidecar

PRIVATE_CIDRS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16")
PROBE_LABEL = "sandboxhub.probe"
_PROBE_PORT = 18080


def _is_loopback(host: str) -> bool:
    if host.strip("[]").lower() in ("localhost", "ip6-localhost"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def chain_names() -> tuple[str, str]:
    """本 Hub 实例的两条自有链（同机多套 Hub 各管各的，链名 ≤ 28 字符）。"""
    digest = hashlib.sha1(
        f"{settings.CONTAINER_LABEL}\0{settings.SANDBOX_GATEWAY_NAME}".encode()
    ).hexdigest()[:8]
    return f"CRSB-{digest}-F", f"CRSB-{digest}-I"


def build_rules(
    bridges: list[BridgeInfo], *, allow_ports: list[int], allow_cidrs: list[str]
) -> str:
    """生成 iptables-restore 输入（filter 表，只声明 / 替换本 Hub 的两条链）。"""
    fwd, inp = chain_names()
    lines = ["*filter", f":{fwd} - [0:0]", f":{inp} - [0:0]", f"-F {fwd}", f"-F {inp}"]
    for b in bridges:
        br, gw = b.bridge, f"{b.gateway_ip}/32"
        lines += [
            f"-A {fwd} -i {br} -o {br} -s {gw} -j ACCEPT",
            f"-A {fwd} -i {br} -o {br} -d {gw} -j ACCEPT",
            f"-A {fwd} -i {br} -s {gw} -j RETURN",
            f"-A {fwd} -i {br} -m conntrack --ctstate DNAT -j DROP",
        ]
        lines += [f"-A {fwd} -i {br} ! -o {br} -d {cidr} -j RETURN" for cidr in allow_cidrs]
        lines += [f"-A {fwd} -i {br} ! -o {br} -d {cidr} -j DROP" for cidr in PRIVATE_CIDRS]
        lines += [
            f"-A {inp} -i {br} -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN",
            f"-A {inp} -i {br} -s {gw} -j RETURN",
        ]
        lines += [f"-A {inp} -i {br} -p tcp --dport {port} -j RETURN" for port in allow_ports]
        lines.append(f"-A {inp} -i {br} -j DROP")
    lines.append("COMMIT")
    return "\n".join(lines) + "\n"


class NetGuard:
    def __init__(self, sidecar: FsSidecar, manager: ContainerManager) -> None:
        self._fs = sidecar
        self._manager = manager
        self._backend: Optional[str] = None
        self._lock = asyncio.Lock()
        self.last_applied: Optional[str] = None

    async def _detect_backend(self) -> str:
        """宿主 Docker 用哪套 iptables 后端：DOCKER-USER 链在哪边就用哪边。"""
        if self._backend:
            return self._backend
        for binary in ("iptables", "iptables-legacy"):
            code, _, _ = await self._fs.exec([binary, "-w", "5", "-S", "DOCKER-USER"])
            if code == 0:
                self._backend = binary
                return binary
        raise RuntimeError("宿主没有 DOCKER-USER 链（Docker 未用 iptables 防火墙后端？），netguard 不可用")

    async def apply(self, bridges: Optional[list[BridgeInfo]] = None) -> str:
        """幂等下发规则；返回规则文本。"""
        async with self._lock:
            bridges = bridges if bridges is not None else await self._manager.wa_bridges()
            binary = await self._detect_backend()
            rules = build_rules(
                bridges,
                allow_ports=settings.netguard_host_allow_ports(),
                allow_cidrs=settings.netguard_egress_allow_cidrs(),
            )
            fwd, inp = chain_names()
            name = f"netguard-{fwd}.rules"
            host_file = self._fs.host_path("run", name)
            await asyncio.to_thread(host_file.write_text, rules)
            restore = "iptables-restore" if binary == "iptables" else "iptables-legacy-restore"
            code, _, err = await self._fs.exec(
                [restore, "-w", "5", "-n", self._fs.container_path("run", name)]
            )
            if code != 0:
                raise RuntimeError(f"netguard 规则下发失败：{err.decode(errors='replace').strip()}")
            script = (
                f"{binary} -w 5 -C DOCKER-USER -j {fwd} 2>/dev/null || {binary} -w 5 -I DOCKER-USER 1 -j {fwd}; "
                f"{binary} -w 5 -C INPUT -j {inp} 2>/dev/null || {binary} -w 5 -I INPUT 1 -j {inp}"
            )
            code, _, err = await self._fs.exec(["sh", "-c", script])
            if code != 0:
                raise RuntimeError(f"netguard 跳转挂接失败：{err.decode(errors='replace').strip()}")
            if rules != self.last_applied:
                logger.info(
                    f"netguard 规则已下发 | chains={fwd},{inp} | bridges={[b.bridge for b in bridges]}"
                )
            self.last_applied = rules
            return rules

    async def verify(self, bridges: list[BridgeInfo]) -> tuple[bool, str]:
        """规则在位：两条跳转存在，且每张 v2 网桥都有 INPUT 兜底丢弃与 DNAT 拦截。"""
        try:
            binary = await self._detect_backend()
        except RuntimeError as exc:
            return False, str(exc)
        fwd, inp = chain_names()
        for chain, target in (("DOCKER-USER", fwd), ("INPUT", inp)):
            code, _, _ = await self._fs.exec([binary, "-w", "5", "-C", chain, "-j", target])
            if code != 0:
                return False, f"{chain} 缺少跳转 {target}"
        code, out_f, _ = await self._fs.exec([binary, "-w", "5", "-S", fwd])
        code_i, out_i, _ = await self._fs.exec([binary, "-w", "5", "-S", inp])
        if code or code_i:
            return False, "自有链不存在"
        text = out_f.decode() + out_i.decode()
        for b in bridges:
            for needle in (
                f"-A {inp} -i {b.bridge} -j DROP",
                f"-A {fwd} -i {b.bridge} -m conntrack --ctstate DNAT -j DROP",
            ):
                if needle not in text:
                    return False, f"缺少规则：{needle}"
        return True, "ok"

    async def teardown(self) -> None:
        """撤下本 Hub 的跳转与自有链（停用 v2 时手动调用）。"""
        binary = await self._detect_backend()
        fwd, inp = chain_names()
        script = (
            f"{binary} -w 5 -D DOCKER-USER -j {fwd} 2>/dev/null; {binary} -w 5 -D INPUT -j {inp} 2>/dev/null; "
            f"{binary} -w 5 -F {fwd} 2>/dev/null; {binary} -w 5 -X {fwd} 2>/dev/null; "
            f"{binary} -w 5 -F {inp} 2>/dev/null; {binary} -w 5 -X {inp} 2>/dev/null; true"
        )
        await self._fs.exec(["sh", "-c", script])


# ── 自检 ────────────────────────────────────────────────────────────────────────

# 探针脚本：并发 TCP 连接目标，打印每个目标的结果。只用标准库，在 cr-fs 镜像里跑。
PROBE_SCRIPT = r"""
import concurrent.futures, json, socket, sys
def probe(t):
    try:
        addr = socket.gethostbyname(t["host"])
    except OSError as exc:
        return dict(t, result="unresolved", error=str(exc))
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect((addr, t["port"]))
        return dict(t, result="open")
    except socket.timeout:
        return dict(t, result="timeout")
    except ConnectionRefusedError:
        return dict(t, result="refused")
    except OSError as exc:
        return dict(t, result="unreachable", error=str(exc))
    finally:
        s.close()
targets = json.loads(sys.argv[1])
with concurrent.futures.ThreadPoolExecutor(32) as pool:
    print(json.dumps(list(pool.map(probe, targets))))
"""


def probe_passes(result: str, expect: str) -> bool:
    """``filtered``：包必须被丢（超时 / 不可达）；``closed``：只要连不上；``open``：必须连上。"""
    if expect == "filtered":
        return result in ("timeout", "unreachable", "unresolved")
    if expect == "closed":
        return result != "open"
    return result == "open"


@dataclass
class Capability:
    ok: bool
    detail: str = ""


@dataclass
class ImageFeatures:
    """v2 实例镜像的能力标签（SandboxHub#52）：是否提供 /api/procs、预置哪些执行引擎及版本。"""

    procs: bool = False
    engines: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_labels(cls, labels: Optional[dict]) -> "ImageFeatures":
        """镜像不存在（None）或缺标签 = 不支持；engines 标签不是 {名字: 版本} 对象时按无引擎处理。"""
        if not labels:
            return cls()
        engines: dict[str, str] = {}
        raw = labels.get(ENGINES_LABEL)
        if raw:
            try:
                value = json.loads(raw)
            except ValueError:
                value = None
            if isinstance(value, dict) and all(
                isinstance(k, str) and k and isinstance(v, str) and v for k, v in value.items()
            ):
                engines = dict(value)
            else:
                logger.warning(f"镜像标签 {ENGINES_LABEL} 不是 {{引擎: 版本}} 对象，按无引擎处理 | value={raw!r}")
        return cls(procs=labels.get(PROCS_LABEL) == "1", engines=engines)


@dataclass
class Capabilities:
    items: dict[str, Capability] = field(default_factory=dict)
    checked_at: Optional[datetime] = None
    duration_ms: int = 0
    # sandbox_type → 镜像能力；随自检刷新（自检前与镜像缺失时为不支持）
    images: dict[str, ImageFeatures] = field(default_factory=dict)

    # 全局必需项：任一不满足 v2 分配一律 503
    REQUIRED = (
        "work_area_enabled",
        "hub_api_auth",
        "hub_bind_loopback",
        "storage_configured",
        "fs_sidecar",
        "mount_propagation",
        "netguard_rules",
        "isolation_peer",
        "isolation_host",
        "isolation_storage",
        "gateway_reachable",
    )

    def failing(self, sandbox_type: Optional[str] = None) -> list[str]:
        if self.checked_at is None:
            return ["selftest_pending"]
        names = list(self.REQUIRED)
        if sandbox_type:
            names.append(f"image_{sandbox_type}")
        return [n for n in names if not self.items.get(n, Capability(False)).ok]

    def ok_for(self, sandbox_type: str) -> bool:
        return not self.failing(sandbox_type)

    def to_dict(self) -> dict:
        features = {t: self.images.get(t) or ImageFeatures() for t in settings.sandbox_types}
        return {
            "ok": not self.failing(),
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "duration_ms": self.duration_ms,
            "failing": self.failing(),
            "items": {k: {"ok": v.ok, "detail": v.detail} for k, v in self.items.items()},
            "sandbox_types": {t: self.ok_for(t) for t in settings.sandbox_types},
            "procs": {t: f.procs for t, f in features.items()},
            "engines": {t: dict(f.engines) for t, f in features.items()},
        }


class SelfTest:
    def __init__(self, sidecar: FsSidecar, netguard: NetGuard, manager: ContainerManager) -> None:
        self._fs = sidecar
        self._netguard = netguard
        self._manager = manager
        self._docker: docker.DockerClient = manager.docker
        self._lock = asyncio.Lock()
        self.result = Capabilities()

    # ── 静态项 ──────────────────────────────────────────────────────────────

    @staticmethod
    def static_items() -> dict[str, Capability]:
        return {
            "work_area_enabled": Capability(settings.WORK_AREA_ENABLED, "work_area.enabled"),
            "hub_api_auth": Capability(
                bool(settings.SANDBOX_HUB_API_KEY), "SANDBOX_HUB_API_KEY 已配置" if settings.SANDBOX_HUB_API_KEY else "SANDBOX_HUB_API_KEY 未配置"
            ),
            "hub_bind_loopback": Capability(
                settings.hub_bound_to_loopback, f"SANDBOX_HUB_HOST={settings.SANDBOX_HUB_HOST}"
            ),
            "storage_configured": Capability(
                bool(settings.MINIO_ENDPOINT and settings.MINIO_ACCESS_KEY and settings.MINIO_SECRET_KEY),
                "MINIO_* 连接信息（cr-fs 挂载与 inputs 下载用）",
            ),
        }

    async def _image_items(self) -> tuple[dict[str, Capability], dict[str, ImageFeatures]]:
        items: dict[str, Capability] = {}
        features: dict[str, ImageFeatures] = {}
        for sandbox_type in settings.sandbox_types:
            image = settings.image_v2_for_type(sandbox_type)
            labels = await self._manager.image_labels(image)
            features[sandbox_type] = ImageFeatures.from_labels(labels)
            if labels is None:
                items[f"image_{sandbox_type}"] = Capability(False, f"镜像 {image} 不存在")
            elif labels.get(API_AUTH_LABEL) != "1":
                items[f"image_{sandbox_type}"] = Capability(False, f"镜像 {image} 缺少标签 {API_AUTH_LABEL}=1")
            else:
                items[f"image_{sandbox_type}"] = Capability(True, image)
        return items, features

    # ── 探针 ────────────────────────────────────────────────────────────────

    def _targets(self, bridge: BridgeInfo, listener_ip: str) -> list[dict]:
        allowed = set(settings.netguard_host_allow_ports())
        host_ports = sorted(
            {settings.SANDBOX_HUB_PORT, 22, 5432, 6379}
            | ({_endpoint_port(settings.MINIO_ENDPOINT)} if settings.MINIO_ENDPOINT else set())
        )
        host_ports = [p for p in host_ports if p not in allowed]
        targets = [{"kind": "peer", "host": listener_ip, "port": _PROBE_PORT, "expect": "filtered"}]
        for host in (bridge.host_ip, "host.docker.internal"):
            targets += [{"kind": "host", "host": host, "port": p, "expect": "filtered"} for p in host_ports]
        if settings.MINIO_ENDPOINT:
            mhost, _, _ = settings.MINIO_ENDPOINT.rpartition(":")
            mhost = mhost or settings.MINIO_ENDPOINT
            mport = _endpoint_port(settings.MINIO_ENDPOINT)
            # 回环地址在容器里指向容器自己，没有探测意义；宿主 IP 上的同一端口已在 host 目标里
            if not _is_loopback(mhost):
                targets.append({"kind": "storage", "host": mhost, "port": mport, "expect": "filtered"})
            targets.append(
                {"kind": "storage", "host": settings.SANDBOX_GATEWAY_NAME, "port": mport, "expect": "closed"}
            )
        forwards = settings.gateway_forwards(include_minio=False)
        if forwards:
            targets.append(
                {"kind": "gateway", "host": settings.SANDBOX_GATEWAY_NAME, "port": forwards[0][0], "expect": "open"}
            )
        return targets

    def _cleanup_probes_sync(self) -> None:
        for c in self._docker.containers.list(
            all=True, filters={"label": f"{PROBE_LABEL}={settings.CONTAINER_LABEL}"}
        ):
            try:
                c.remove(force=True)
            except docker.errors.APIError:
                pass

    def _run_probes_sync(self, bridges: list[BridgeInfo]) -> dict[str, list[dict]]:
        image = settings.DOCKER_IMAGE_FS
        labels = {PROBE_LABEL: settings.CONTAINER_LABEL}
        self._cleanup_probes_sync()
        listener = self._docker.containers.run(
            image=image,
            command=["python3", "-m", "http.server", str(_PROBE_PORT)],
            detach=True,
            network=bridges[0].network,
            labels=labels,
            cap_drop=["NET_RAW", "MKNOD"],
            security_opt=["no-new-privileges"],
        )
        try:
            for b in bridges[1:]:
                self._docker.networks.get(b.network).connect(listener)
            listener.reload()
            nets = listener.attrs["NetworkSettings"]["Networks"]
            ips = {b.network: nets[b.network]["IPAddress"] for b in bridges}
            # 阳性对照：宿主能连上探针监听端口，证明「连不上」来自隔离而不是监听没起来。
            deadline = time.time() + 10
            while True:
                try:
                    with socket.create_connection((ips[bridges[0].network], _PROBE_PORT), timeout=1):
                        break
                except OSError:
                    if time.time() > deadline:
                        raise RuntimeError("探针监听容器未就绪")
                    time.sleep(0.2)
            results: dict[str, list[dict]] = {}
            for b in bridges:
                out = self._docker.containers.run(
                    image=image,
                    command=["python3", "-c", PROBE_SCRIPT, json.dumps(self._targets(b, ips[b.network]))],
                    network=b.network,
                    labels=labels,
                    remove=True,
                    extra_hosts={"host.docker.internal": "host-gateway"},
                    cap_drop=["NET_RAW", "MKNOD"],
                    security_opt=["no-new-privileges"],
                )
                results[b.network] = json.loads(out.decode().strip().splitlines()[-1])
            return results
        finally:
            try:
                listener.remove(force=True)
            except docker.errors.APIError:
                pass

    @staticmethod
    def summarize(results: dict[str, list[dict]]) -> dict[str, Capability]:
        kinds = {"peer": "isolation_peer", "host": "isolation_host", "storage": "isolation_storage", "gateway": "gateway_reachable"}
        failures: dict[str, list[str]] = {name: [] for name in kinds.values()}
        seen: set[str] = set()
        for network, rows in results.items():
            for row in rows:
                item = kinds[row["kind"]]
                seen.add(item)
                if not probe_passes(row["result"], row["expect"]):
                    failures[item].append(f"{network}→{row['host']}:{row['port']}={row['result']}")
        items: dict[str, Capability] = {}
        for item in kinds.values():
            if item == "gateway_reachable" and item not in seen:
                items[item] = Capability(True, "未配置网关转发，跳过阳性对照")
            elif failures[item]:
                items[item] = Capability(False, "; ".join(failures[item][:8]))
            else:
                items[item] = Capability(True, "ok")
        return items

    # ── 入口 ────────────────────────────────────────────────────────────────

    async def run(self) -> Capabilities:
        async with self._lock:
            started = time.monotonic()
            items = self.static_items()
            image_items, images = await self._image_items()
            items.update(image_items)
            fs_ok = await self._fs.running()
            items["fs_sidecar"] = Capability(fs_ok, self._fs.name if fs_ok else f"{self._fs.name} 未运行")
            probe_names = ("isolation_peer", "isolation_host", "isolation_storage", "gateway_reachable")
            if fs_ok:
                ok, detail = await self._fs.probe_propagation()
                items["mount_propagation"] = Capability(ok, detail)
                try:
                    bridges = await self._manager.wa_bridges()
                    ok, detail = await self._netguard.verify(bridges)
                    items["netguard_rules"] = Capability(ok, detail)
                    results = await asyncio.to_thread(self._run_probes_sync, bridges)
                    items.update(self.summarize(results))
                except Exception as exc:
                    items.setdefault("netguard_rules", Capability(False, str(exc)))
                    for name in probe_names:
                        items.setdefault(name, Capability(False, f"自检未完成：{exc}"))
            else:
                for name in ("mount_propagation", "netguard_rules", *probe_names):
                    items[name] = Capability(False, "cr-fs 未运行")
            result = Capabilities(
                items=items,
                checked_at=datetime.now(tz=timezone.utc),
                duration_ms=int((time.monotonic() - started) * 1000),
                images=images,
            )
            failing = result.failing()
            if failing:
                logger.warning(f"v2 隔离自检未通过 | failing={failing}")
            else:
                logger.info(f"v2 隔离自检通过 | duration={result.duration_ms}ms")
            self.result = result
            return result
