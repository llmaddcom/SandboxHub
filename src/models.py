import re
"""
SandboxHub 共享数据模型

ContainerInfo：运行中容器的基础信息（由 ContainerManager 产出）
SandboxRecord：已分配沙盒的完整记录（由 Registry 管理）
ManagedContainer：Docker 侧受管容器的对账视图（由 ContainerManager.list_managed 产出）
"""
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

SandboxType = Literal["ubuntu", "code"]
SandboxStatus = Literal["ready", "released"]
# 网络策略二值（createrole#449 / SandboxHub#42）：allow=联网态网络，deny=隔离网。
NetworkMode = Literal["allow", "deny"]

# v2 实例（按工作区分配，ADR 0025）的容器标签：Hub 重启后据此恢复在册记录。
V2_LABEL = "sandboxhub.v2"
WORK_AREA_LABEL = "sandboxhub.work-area"
SANDBOX_ID_LABEL = "sandboxhub.sandbox-id"
USER_LABEL = "sandboxhub.user"
ROLE_LABEL = "sandboxhub.role"
SESSION_LABEL = "sandboxhub.session"
MOUNT_LAYOUT_LABEL = "sandboxhub.mount-layout"
# 实例的目录布局版本（#71）：缺省视为 1（引入本标签之前创建的实例都是 v1）。
LAYOUT_VERSION_LABEL = "sandboxhub.layout-version"
# /tmp 绑定到工作区宿主目录的实例（请求带 tmp 规则）：Hub 重启收养后仍知道回收时要按规则清理。
TMP_BOUND_LABEL = "sandboxhub.tmp-bound"

# 布局 v2（#71，file-up）的角色目录：``self`` = 角色空间根（排除平台保留区 ``.cr/``），
# ``diary`` = 叠在其上的只读日记。v1 的角色目录一律在 ``/workspace/role/<name>``。
V2_ROLE_TARGETS = {"self": "/self", "diary": "/self/diary"}
V2_SELF_EXCLUDE = ("/.cr/**",)
# 平台保留目录：v2 实例里 /self 下列不出、经文件端点写入会被拒绝。
V2_SELF_HIDDEN = ("/self/.cr",)
# 后端随 ``self`` 挂载附带的排除规则（#73）：相对角色空间根的目录子树，如 ``avatar/**``；
# 开头的 ``/`` 可有可无（同样相对 /self 根）。只接受「字面路径」或「字面路径/**」，平台据此
# 推出要隐藏的目录，保证列不出、也写不进。
SELF_EXCLUDE_MAX = 16
_EXCLUDE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


def normalize_self_exclude(patterns) -> tuple[str, ...]:
    """校验并规整 ``self`` 挂载的排除规则，返回相对路径形式（如 ``avatar/**``），去重保序。

    非法时抛 ``ValueError``（调用方映射为 ``invalid_mount``）。
    """
    if patterns is None:
        return ()
    if isinstance(patterns, (str, bytes)) or not isinstance(patterns, (list, tuple)):
        raise ValueError("exclude 须是字符串列表")
    if len(patterns) > SELF_EXCLUDE_MAX:
        raise ValueError(f"exclude 最多 {SELF_EXCLUDE_MAX} 条")
    out: list[str] = []
    for raw in patterns:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"exclude 条目非法：{raw!r}")
        value = raw.strip()
        if value.startswith("/"):
            value = value[1:]
        if value.startswith("/"):
            raise ValueError(f"exclude 只能是相对 /self 的路径：{raw!r}")
        body = value[:-3] if value.endswith("/**") else value
        segments = body.split("/")
        if not body or any(
            seg in ("", ".", "..") or not _EXCLUDE_SEGMENT.match(seg) for seg in segments
        ):
            raise ValueError(f"exclude 条目非法（只接受相对目录或「目录/**」，不含 .. 与通配）：{raw!r}")
        rule = f"{body}/**"
        if rule not in out:
            out.append(rule)
    return tuple(out)


@dataclass
class WorkspaceMount:
    """一次工作区挂载请求：把 MinIO 的 ``bucket/prefix`` 实时挂到容器 ``mount_path``。

    由 acquire 请求体的 ``workspace`` 字段构造；createrole 侧 = 持久事实源（MinIO），
    SandboxHub = 运行时（容器内 rclone 挂载 + MinIO↔容器传输）。
    """
    bucket: str
    prefix: str
    mount_path: str = "/workspace"


@dataclass
class ContainerInfo:
    container_id: str
    container_name: str
    container_ip: str
    sandbox_type: SandboxType
    # 是否挂载了工作区（云盘）。挂载容器不进 warm pool、释放即销毁，且清理时绝不
    # 对挂载点做 rm -rf（会误删 MinIO 数据），只卸载后销毁。
    mounted: bool = False
    mount_path: str = "/workspace"
    # 是否注入过调用方环境变量（issue #15/#16，值可能是租户 scoped token）。
    # 注入的 env 无法从运行中容器清除，故该容器同样不入 warm pool、释放即销毁，
    # 防止凭据泄漏给下一个租户。
    env_injected: bool = False
    inputs_mounted: bool = False
    # 容器当前所在 Docker 网络名（联网态 SANDBOX_NETWORK / 隔离网 SANDBOX_NETWORK_ISOLATED）。
    # 空 = 未知（旧记录 / 测试构造），switch_network 会按实际状态处理。
    network: str = ""


@dataclass
class SandboxRecord:
    sandbox_id: str
    container_info: ContainerInfo
    user_id: str
    role_id: str
    status: SandboxStatus
    acquired_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    # 最近一次被使用的时间（acquire 复用 / proxy 转发时刷新）：闲置回收与 released 记录
    # 修剪都以此为准。
    last_active_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


@dataclass
class ManagedContainer:
    """Docker 侧一个受管容器（带 sandboxhub.managed 标签）的对账视图。

    与 ContainerInfo 的区别：这是「Docker 里实际存在什么」的原始事实（含已停止的、
    不在册的），供 Reconciler 与 registry/pool 的在册状态对账；ContainerInfo 是
    「SandboxHub 认领并可分配」的运行时句柄。
    """
    container_id: str
    container_name: str
    status: str  # Docker 容器状态：running/exited/created/dead/...
    sandbox_type: str
    mounted: bool
    created_at: Optional[datetime]  # Docker Created 时间；解析失败为 None（视为刚创建）
    container_ip: str = ""  # 仅 running 且拿得到 IP 时非空
    env_injected: bool = False  # 注入过租户 env 的容器：启动恢复不收养回池，直接销毁
    network: str = ""  # 容器实际所在 Docker 网络名（running 时），供 registry 网络对齐
    # v2 实例（按工作区分配）的标签与环境：v1 对账逻辑整体跳过 v2 容器，由 v2 对账处理。
    labels: dict[str, str] = field(default_factory=dict)
    api_token: str = ""  # v2 实例的每实例 API token（容器 env SANDBOX_API_TOKEN），用于重启恢复

    @property
    def is_v2(self) -> bool:
        return self.labels.get(V2_LABEL) == "true"


# ── v2：按工作区分配的执行实例（ADR 0025）──────────────────────────────────────

RoleMountMode = Literal["ro", "rw"]


@dataclass(frozen=True)
class RoleMountSpec:
    """一个角色空间目录：MinIO ``bucket/prefix`` 经 cr-fs rclone 挂载，再 bind 到实例里的
    ``target``（v1：``/workspace/role/<name>``；v2：见 ``V2_ROLE_TARGETS``）。
    同一 (bucket, prefix, mode, exclude) 的多个实例共用一个挂载。"""
    name: str
    bucket: str
    prefix: str
    mode: RoleMountMode = "ro"
    layout_version: int = 1
    # 后端附带的排除规则（相对形式，见 ``normalize_self_exclude``）；只对 v2 ``self`` 有效
    extra_exclude: tuple[str, ...] = ()

    @property
    def is_v2_self(self) -> bool:
        return self.layout_version >= 2 and self.name == "self"

    @property
    def exclude(self) -> tuple[str, ...]:
        """rclone 过滤规则：v2 的 ``self`` 挂整个角色空间，平台保留区 ``.cr/`` 与后端附带的
        目录（``extra_exclude``）不进实例。"""
        if not self.is_v2_self:
            return ()
        rules = list(V2_SELF_EXCLUDE)
        for rule in self.extra_exclude:
            anchored = f"/{rule}"
            if anchored not in rules:
                rules.append(anchored)
        return tuple(rules)

    @property
    def hidden_dirs(self) -> tuple[str, ...]:
        """实例里要按平台保留目录拒绝写入的路径（容器内绝对路径）。"""
        if not self.is_v2_self:
            return ()
        dirs = list(V2_SELF_HIDDEN)
        for rule in self.extra_exclude:
            path = f"{self.target}/{rule.removesuffix('/**')}"
            if path not in dirs:
                dirs.append(path)
        return tuple(dirs)

    @property
    def key(self) -> str:
        raw = f"{self.bucket}\0{self.prefix.strip('/')}\0{self.mode}"
        if self.exclude:
            # 只有带过滤规则的挂载才加这一段：v1 与 v2 diary 的 key 不变，可共用同一挂载。
            raw += "\0exclude=" + ",".join(self.exclude)
        return hashlib.sha256(raw.encode()).hexdigest()[:20]

    @property
    def target(self) -> str:
        if self.layout_version >= 2:
            return V2_ROLE_TARGETS.get(self.name, f"/self/{self.name}")
        return f"/workspace/role/{self.name}"


@dataclass(frozen=True)
class InputFileSpec:
    """inputs 里的一个文件：用户空间 blob（按 sha256 寻址），以 ``path`` 出现在 inputs 视图（v1 /workspace/inputs，v2 /mnt/drive）。"""
    path: str
    bucket: str
    key: str
    sha256: str
    size: int


@dataclass
class WorkAreaInstance:
    """v2 执行实例在册记录（按 work_area_id 唯一）。

    ``status``：starting = 正在创建（占名额，防并发越限）；ready = 可用。
    释放即销毁容器（不回池），工作区目录保留。
    """
    sandbox_id: str
    work_area_id: str
    user_id: str
    role_id: str
    sandbox_type: SandboxType
    api_token: str
    status: Literal["starting", "ready", "quiesced"] = "starting"
    container_info: Optional[ContainerInfo] = None
    mounts: tuple[RoleMountSpec, ...] = ()
    session_id: str = ""
    turn_id: str = ""
    network_mode: NetworkMode = "allow"
    # 目录布局版本（#71）：1 = /workspace/{work,inputs,context,role}；2 = /self /workspace /mnt/drive /opt/cr
    layout_version: int = 1
    # 是否持有角色目录挂载引用（创建中途失败时尚未取得，释放时不得多减）
    mounts_held: bool = False
    # /tmp 是否绑定到工作区的宿主目录（请求带 tmp 规则时）；回收时据此决定要不要按规则清理
    tmp_bound: bool = False
    acquired_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    last_active_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))

    @property
    def mount_keys(self) -> tuple[str, ...]:
        return tuple(sorted(m.key for m in self.mounts))

    def holder(self) -> dict:
        """409 忙碌 / 超限时返回给调用方的占用者描述。"""
        return {
            "sandbox_id": self.sandbox_id,
            "work_area_id": self.work_area_id,
            "user_id": self.user_id,
            "role_id": self.role_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "status": self.status,
            "acquired_at": self.acquired_at.isoformat(),
            "last_active_at": self.last_active_at.isoformat(),
        }
