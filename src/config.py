"""
SandboxHub 配置（issue #28，对齐 createrole#400「三个家」口径）

每个键只有一个主人、一个可写位置：

- 部署接入（连接地址 / 密钥 / 本机与网络属性）：``.env``（不进 git），pydantic-settings 读 env。
- 系统调优（镜像名 / 预热池 / 对账回收 / 代理超时 / rclone 挂载策略）：``config/system.yaml``
  （进 git，改动走 PR）。取值：文件 > 代码默认；缺键 / null / 坏值仅该项回退并告警。
  系统键**没有** env 覆盖口：同名旧 ENV 已退役，启动期告警并忽略。

提供全局单例 ``settings``；``load_settings()`` 供测试指定 yaml / env 文件。
"""
from __future__ import annotations

import ipaddress
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from loguru import logger
from pydantic import TypeAdapter, ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_YAML_PATH = REPO_ROOT / "config" / "system.yaml"


@dataclass(frozen=True)
class SystemKnob:
    """系统键声明：``key`` 是 system.yaml 里的 ``section.name``，``field`` 是 Settings 字段名。"""

    key: str
    field: str
    description: str


# 系统键声明表：一处声明，system.yaml 对账 / 退役 ENV 清单 / yaml 装载 都从这里推导。
SYSTEM_KNOBS: tuple[SystemKnob, ...] = (
    SystemKnob("image.ubuntu", "DOCKER_IMAGE_UBUNTU", "desktop profile 镜像名"),
    SystemKnob("image.code", "DOCKER_IMAGE_CODE", "code profile 镜像名"),
    SystemKnob("image.gateway", "DOCKER_IMAGE_GATEWAY", "cr-host 网关容器镜像名（网络策略平台通道）"),
    SystemKnob("warm_pool.ubuntu", "WARM_POOL_UBUNTU", "ubuntu 预热容器数"),
    SystemKnob("warm_pool.code", "WARM_POOL_CODE", "code 预热容器数"),
    SystemKnob("warm_pool.maintain_interval", "POOL_MAINTAIN_INTERVAL", "预热池补齐检查间隔（秒）"),
    SystemKnob("sandbox.api_port", "SANDBOX_API_PORT", "容器内沙盒 FastAPI 端口"),
    SystemKnob("sandbox.idle_ttl", "SANDBOX_IDLE_TTL", "已分配沙盒闲置回收阈值（秒，0=关闭）"),
    SystemKnob("sandbox.file_upload_max_bytes", "FILE_UPLOAD_MAX_BYTES", "容器文件上传上限（字节，0=不限）"),
    SystemKnob("reconcile.interval", "RECONCILE_INTERVAL", "周期对账间隔（秒）"),
    SystemKnob("reconcile.orphan_grace_seconds", "ORPHAN_GRACE_SECONDS", "孤儿容器创建宽限（秒）"),
    SystemKnob("proxy.read_timeout", "PROXY_READ_TIMEOUT", "代理读超时（秒）"),
    SystemKnob("proxy.connect_timeout", "PROXY_CONNECT_TIMEOUT", "代理连接超时（秒）"),
    SystemKnob("workspace.mount_enabled", "WORKSPACE_MOUNT_ENABLED", "工作区挂载总开关"),
    SystemKnob("workspace.rclone_vfs_cache_mode", "RCLONE_VFS_CACHE_MODE", "rclone VFS 缓存模式"),
    SystemKnob("workspace.rclone_vfs_cache_max_size", "RCLONE_VFS_CACHE_MAX_SIZE", "VFS 缓存体积上限"),
    SystemKnob("workspace.rclone_vfs_write_back", "RCLONE_VFS_WRITE_BACK", "文件关闭后回写延迟"),
    SystemKnob("workspace.rclone_dir_cache_time", "RCLONE_DIR_CACHE_TIME", "目录列表缓存时长"),
    SystemKnob("workspace.mount_ready_retries", "MOUNT_READY_RETRIES", "挂载就绪探测轮询次数"),
    SystemKnob("workspace.mount_ready_interval", "MOUNT_READY_INTERVAL", "挂载就绪探测间隔（秒）"),
    SystemKnob("gateway.minio_forward", "GATEWAY_MINIO_FORWARD", "v1 网关是否自动转发 MinIO（v2 网关从不转发）"),
    SystemKnob("work_area.enabled", "WORK_AREA_ENABLED", "v2 按工作区分配执行实例的总开关"),
    SystemKnob("work_area.image_code", "DOCKER_IMAGE_CODE_V2", "v2 code 实例镜像（须带 sandboxhub.api_auth=1）"),
    SystemKnob("work_area.image_ubuntu", "DOCKER_IMAGE_UBUNTU_V2", "v2 desktop 实例镜像（须带 sandboxhub.api_auth=1）"),
    SystemKnob("work_area.image_fs", "DOCKER_IMAGE_FS", "cr-fs 特权 sidecar 镜像"),
    SystemKnob("work_area.max_per_role", "WORK_AREA_MAX_PER_ROLE", "每个数字人同时存活的实例上限"),
    SystemKnob("work_area.max_per_user", "WORK_AREA_MAX_PER_USER", "每个用户同时存活的实例上限"),
    SystemKnob("work_area.job_max_lifetime", "WORK_AREA_JOB_MAX_LIFETIME", "实例内单个后台 job 最长存活（秒，0=不限）"),
    SystemKnob("work_area.idle_backstop_ttl", "WORK_AREA_IDLE_BACKSTOP_TTL", "Hub 兜底回收：无 API 往来超过此时长删实例（秒，0=关闭）"),
    SystemKnob("work_area.idle_release_seconds", "WORK_AREA_IDLE_RELEASE_SECONDS", "平台侧空闲回收：回合结束且无后台 job 后空闲多久释放实例（秒，经 capabilities 下发给平台）"),
    SystemKnob("work_area.soft_cap_bytes", "WORK_AREA_SOFT_CAP_BYTES", "工作区软上限（字节，平台在写类工具执行前检查，0=不限）"),
    SystemKnob("work_area.selftest_interval", "WORK_AREA_SELFTEST_INTERVAL", "隔离自检周期（秒）"),
    SystemKnob("work_area.inputs_cache_max_bytes", "INPUTS_CACHE_MAX_BYTES", "inputs blob 缓存总量上限（字节，0=不限）"),
    SystemKnob("work_area.unmount_grace_seconds", "ROLE_MOUNT_UNMOUNT_GRACE", "角色目录挂载引用归零后延迟卸载（秒）"),
    SystemKnob("work_area.list_max_files", "WORK_AREA_LIST_MAX_FILES", "工作区文件列表单次最多条目"),
    SystemKnob("work_area.usage_cache_seconds", "WORK_AREA_USAGE_CACHE_SECONDS", "工作区用量统计缓存（秒）"),
)
SYSTEM_FIELDS: frozenset[str] = frozenset(k.field for k in SYSTEM_KNOBS)
# 退役的 ENV：与系统键同名的旧覆盖口。启动期发现即告警并忽略（一个版本后删清单）。
RETIRED_ENV_KEYS: frozenset[str] = SYSTEM_FIELDS

_MISSING = object()
# Docker 内置网络：没有内嵌 DNS 按名解析，网络策略在这些网络上不可用。
_DOCKER_BUILTIN_NETWORKS = frozenset({"bridge", "host", "none"})


def _endpoint_port(endpoint: str, default: int = 9000) -> int:
    """从 ``host:port`` 取端口；无端口用默认（MinIO 9000）。"""
    host, sep, port = endpoint.rpartition(":")
    if sep and port.isdigit() and host:
        return int(port)
    return default
# load_settings() 借此把 yaml 路径传进 settings_customise_sources（pydantic 无逐实例参数口）。
_system_yaml_ctx: ContextVar[Path] = ContextVar("sandboxhub_system_yaml", default=SYSTEM_YAML_PATH)


def _load_system_yaml(path: Path) -> dict[str, Any]:
    """装载 system.yaml；缺文件 / 解析失败整体回退空 dict（全部系统键用代码默认）并告警。"""
    if not path.exists():
        logger.warning("系统调优文件 {} 不存在，系统键全部用代码默认", path)
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("系统调优文件 {} 解析失败，系统键全部用代码默认：{}", path, exc)
        return {}
    if not isinstance(raw, dict):
        logger.warning("系统调优文件 {} 顶层不是映射，系统键全部用代码默认", path)
        return {}
    return raw


def _dig(data: dict[str, Any], key: str) -> Any:
    section, name = key.split(".", 1)
    body = data.get(section)
    if not isinstance(body, dict) or name not in body:
        return _MISSING
    return body[name]


def _coerce(annotation: Any, raw: Any) -> Any:
    """按字段类型把 yaml 标量收敛成字段值；不合法抛 ValueError。"""
    if annotation is str:
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
            raise ValueError(f"期望字符串，得到 {type(raw).__name__}")
        return str(raw)
    try:
        return TypeAdapter(annotation).validate_python(raw)
    except ValidationError as exc:
        raise ValueError(exc.errors()[0].get("msg", str(exc))) from exc


class _SystemYamlSource(PydanticBaseSettingsSource):
    """系统键取值器：只读 config/system.yaml；每键独立回退默认并告警。"""

    def __init__(self, settings_cls: type[BaseSettings], path: Path):
        super().__init__(settings_cls)
        self.path = path

    def get_field_value(self, field, field_name):  # pragma: no cover - 走 __call__
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        data = _load_system_yaml(self.path)
        out: dict[str, Any] = {}
        if not data:
            return out
        fields = self.settings_cls.model_fields
        for knob in SYSTEM_KNOBS:
            raw = _dig(data, knob.key)
            default = fields[knob.field].default
            if raw is _MISSING:
                logger.warning("system.yaml 缺键 {}，用代码默认 {!r}", knob.key, default)
                continue
            if raw is None:
                continue  # null = 显式沿用代码默认
            try:
                out[knob.field] = _coerce(fields[knob.field].annotation, raw)
            except ValueError as exc:
                logger.warning(
                    "system.yaml 键 {} 值 {!r} 非法（{}），用代码默认 {!r}", knob.key, raw, exc, default
                )
        return out


class _WithoutSystemKeys(PydanticBaseSettingsSource):
    """包装 env / dotenv 取值器：剔除系统键（退役 ENV），命中即告警并忽略。"""

    def __init__(self, inner: PydanticBaseSettingsSource):
        super().__init__(inner.settings_cls)
        self.inner = inner

    def get_field_value(self, field, field_name):  # pragma: no cover - 走 __call__
        return self.inner.get_field_value(field, field_name)

    def __call__(self) -> dict[str, Any]:
        values = self.inner()
        retired = sorted(k for k in values if k in RETIRED_ENV_KEYS)
        if retired:
            logger.warning(
                "以下环境变量已退役（系统键只认 config/system.yaml，不再有 env 覆盖口），值被忽略：{}",
                ", ".join(retired),
            )
        return {k: v for k, v in values.items() if k not in RETIRED_ENV_KEYS}


class Settings(BaseSettings):
    # ── 部署接入（.env）：本机 ───────────────────────────────────────────────
    SANDBOX_HUB_PORT: int = 8088
    # 监听地址：默认只绑本机回环。v2 要求回环 + API key，否则 /v2/capabilities 判不安全。
    SANDBOX_HUB_HOST: str = "127.0.0.1"
    # 受管容器标签（同机多实例隔离用，属本机属性）
    CONTAINER_LABEL: str = "sandboxhub.managed"
    # v2 宿主数据根：工作区、实例 inputs/context、blob 缓存、角色目录挂载点都在其下。
    # 空 = 仓库下 var/sandboxhub。所在文件系统须是 shared 挂载传播（findmnt -o PROPAGATION），
    # cr-fs 里的 rclone 挂载才能传播到宿主再 bind 给实例。
    WORK_AREA_ROOT: str = ""

    # ── 部署接入（.env）：网络 ───────────────────────────────────────────────
    # 容器所在 Docker 网络（联网态）。须是用户自定义 bridge 网络（Hub 启动时幂等创建）：
    # 网络策略靠 Docker 内嵌 DNS 按容器名解析网关容器 cr-host，Docker 默认 `bridge`
    # 没有这能力。配成 bridge/host/none 时网络策略整体不可用：acquire 请求 deny 一律
    # 400 拒绝（fail-closed），联网态行为与旧版一致。
    SANDBOX_NETWORK: str = "cr-sb-net"
    # 断网态网络：`--internal`，无默认路由、无 NAT、Docker ≥28 连宿主也不可达；容器只能
    # 到同网容器（即 cr-host 网关）。acquire 携带 policy.network.default=deny 的沙盒
    # 热切到这张网（运行中容器 disconnect/connect，亚秒，进程不重启）。
    SANDBOX_NETWORK_ISOLATED: str = "cr-sb-isolated"
    # 网关容器名 = 容器内访问平台通道（MinIO / createrole API）的固定主机名，两张网
    # 都能按名解析；切网后无需改 /etc/hosts。也是后续 egress 代理（域名白名单）的落位。
    SANDBOX_GATEWAY_NAME: str = "cr-host"
    # 网关额外端口转发：逗号分隔 `listen=upstream_host:port`，upstream 写宿主可达地址
    # （网关容器带 host.docker.internal → 宿主）。MinIO 转发按 MINIO_ENDPOINT 自动加入。
    # 例：SANDBOX_GATEWAY_FORWARDS=8012=host.docker.internal:8012（createrole API 给容器内 CLI）
    SANDBOX_GATEWAY_FORWARDS: str = ""
    # 容器代理：空=不注入代理。容器内 127.0.0.1 不是宿主，需用 host.docker.internal
    # 或宿主在 docker 网桥上可达的地址，例：http://host.docker.internal:8118
    SANDBOX_HTTP_PROXY: str = ""
    # 容器自定义 DNS：逗号分隔，空=用 Docker 默认 DNS
    SANDBOX_DNS: str = ""
    # 保留 Docker 注入的 resolv.conf：ubuntu 镜像 entrypoint 默认把 /etc/resolv.conf 覆写为
    # 8.8.8.8/1.1.1.1（会顶掉 --dns 注入）。SANDBOX_DNS 非空时自动向容器注入
    # SANDBOX_KEEP_DNS=1 跳过覆写；此开关允许在不配 SANDBOX_DNS 时也强制保留。
    SANDBOX_KEEP_DNS: bool = False
    # v2 隔离（netguard）：实例默认连不上宿主任何端口、其他沙盒和私网地址，只能经网关。
    # 逗号分隔的宿主端口白名单（实例可直连宿主的端口，如宿主上的 HTTP 代理端口）。
    # SANDBOX_HTTP_PROXY 指向 host.docker.internal 时其端口自动加入。
    NETGUARD_HOST_ALLOW_PORTS: str = ""
    # 逗号分隔的私网 CIDR 白名单（默认私网 / CGNAT / 链路本地全部拦截）。
    NETGUARD_EGRESS_ALLOW_CIDRS: str = ""

    # ── 部署接入（.env）：对象存储连接 ───────────────────────────────────────
    # rclone S3 remote 指向的 MinIO 连接信息（留空=不具备挂载能力，acquire 里的
    # workspace 被忽略并记 warning，回退为不挂载容器）。
    MINIO_ENDPOINT: str = ""          # host:port，例：minio:9000（不含 scheme）
    MINIO_ACCESS_KEY: str = ""
    MINIO_SECRET_KEY: str = ""
    MINIO_SECURE: bool = False        # True=https

    # ── 部署接入（.env）：密钥 ───────────────────────────────────────────────
    # API 鉴权（可选）：非空时所有请求须带 X-API-Key 头且匹配，否则 401；/v1/health 豁免。
    # 空 = 完全不鉴权。createrole 客户端用同名 env SANDBOX_HUB_API_KEY 配置。
    SANDBOX_HUB_API_KEY: str = ""

    # ── 系统调优（config/system.yaml）：以下为代码默认兜底，env 不读 ─────────
    # 各键含义见 SYSTEM_KNOBS 与 config/system.yaml 内注释。
    DOCKER_IMAGE_UBUNTU: str = "sandbox-ubuntu:latest"
    DOCKER_IMAGE_CODE: str = "sandbox-code:latest"
    DOCKER_IMAGE_GATEWAY: str = "sandbox-gateway:latest"
    WARM_POOL_UBUNTU: int = 3
    WARM_POOL_CODE: int = 0
    POOL_MAINTAIN_INTERVAL: int = 30
    SANDBOX_API_PORT: int = 8000
    SANDBOX_IDLE_TTL: int = 7200
    FILE_UPLOAD_MAX_BYTES: int = 0
    RECONCILE_INTERVAL: int = 60
    ORPHAN_GRACE_SECONDS: int = 300
    PROXY_READ_TIMEOUT: float = 330.0
    PROXY_CONNECT_TIMEOUT: float = 10.0
    WORKSPACE_MOUNT_ENABLED: bool = True
    RCLONE_VFS_CACHE_MODE: str = "full"
    RCLONE_VFS_WRITE_BACK: str = "1s"
    RCLONE_VFS_CACHE_MAX_SIZE: str = "2G"
    RCLONE_DIR_CACHE_TIME: str = "2s"
    MOUNT_READY_RETRIES: int = 20
    MOUNT_READY_INTERVAL: float = 0.5
    GATEWAY_MINIO_FORWARD: bool = True
    WORK_AREA_ENABLED: bool = True
    DOCKER_IMAGE_CODE_V2: str = "sandbox-code:2026.10.25-1"
    DOCKER_IMAGE_UBUNTU_V2: str = "sandbox-ubuntu:2026.10.25-1"
    DOCKER_IMAGE_FS: str = "sandbox-fs:2026.10.06-1"
    WORK_AREA_MAX_PER_ROLE: int = 2
    WORK_AREA_MAX_PER_USER: int = 4
    WORK_AREA_JOB_MAX_LIFETIME: int = 21600
    WORK_AREA_IDLE_BACKSTOP_TTL: int = 86400
    WORK_AREA_IDLE_RELEASE_SECONDS: int = 900
    WORK_AREA_SOFT_CAP_BYTES: int = 10 * 1024**3
    WORK_AREA_SELFTEST_INTERVAL: int = 600
    INPUTS_CACHE_MAX_BYTES: int = 20 * 1024**3
    ROLE_MOUNT_UNMOUNT_GRACE: int = 60
    WORK_AREA_LIST_MAX_FILES: int = 5000
    WORK_AREA_USAGE_CACHE_SECONDS: int = 15

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        # 优先级：显式入参（测试用）> 进程 env > .env > system.yaml > 代码默认。
        # env / .env 剔除系统键，系统键只能来自 system.yaml。
        return (
            init_settings,
            _WithoutSystemKeys(env_settings),
            _WithoutSystemKeys(dotenv_settings),
            _SystemYamlSource(settings_cls, _system_yaml_ctx.get()),
            file_secret_settings,
        )

    @property
    def sandbox_types(self) -> tuple[str, ...]:
        return ("ubuntu", "code")

    def image_for_type(self, sandbox_type: str) -> str:
        mapping = {"ubuntu": self.DOCKER_IMAGE_UBUNTU, "code": self.DOCKER_IMAGE_CODE}
        if sandbox_type not in mapping:
            raise ValueError(f"未知 sandbox_type: {sandbox_type}")
        return mapping[sandbox_type]

    def pool_size_for_type(self, sandbox_type: str) -> int:
        mapping = {"ubuntu": self.WARM_POOL_UBUNTU, "code": self.WARM_POOL_CODE}
        return mapping.get(sandbox_type, 0)

    def dns_servers(self) -> list[str]:
        return [s.strip() for s in self.SANDBOX_DNS.split(",") if s.strip()]

    # ── 网络策略（createrole#449 / SandboxHub#42）────────────────────────────

    @property
    def network_policy_supported(self) -> bool:
        """网络策略可用条件：联网态网络是用户自定义网络（Docker 内嵌 DNS 才能按名解析网关）。"""
        return self.SANDBOX_NETWORK not in _DOCKER_BUILTIN_NETWORKS

    def network_for_mode(self, mode: str) -> str:
        """策略 → 容器应在的 Docker 网络：deny=隔离网，其余=联网态网络。"""
        return self.SANDBOX_NETWORK_ISOLATED if mode == "deny" else self.SANDBOX_NETWORK

    def gateway_forwards(self, *, include_minio: bool | None = None) -> list[tuple[int, str]]:
        """网关容器的端口转发表 ``[(listen_port, "upstream_host:port")]``。

        MinIO 转发按 ``MINIO_ENDPOINT`` 自动加入（监听端口 = MinIO 端口，上游 = MINIO_ENDPOINT
        原值，宿主可达地址），受 ``gateway.minio_forward`` 控制；v2 网关传 ``include_minio=False``
        （实例不持有存储凭据，也不该能连到 MinIO）。其余来自 ``SANDBOX_GATEWAY_FORWARDS``。
        坏条目告警并跳过。
        """
        if include_minio is None:
            include_minio = self.GATEWAY_MINIO_FORWARD and not self.files_cutover_active
        forwards: dict[int, str] = {}
        if self.MINIO_ENDPOINT and include_minio:
            forwards[_endpoint_port(self.MINIO_ENDPOINT)] = self.MINIO_ENDPOINT
        for raw in self.SANDBOX_GATEWAY_FORWARDS.split(","):
            raw = raw.strip()
            if not raw:
                continue
            listen, sep, upstream = raw.partition("=")
            try:
                port = int(listen)
                if not sep or ":" not in upstream or not (0 < port < 65536):
                    raise ValueError
            except ValueError:
                logger.warning("SANDBOX_GATEWAY_FORWARDS 条目非法，跳过：{!r}（期望 listen=host:port）", raw)
                continue
            forwards[port] = upstream.strip()
        if not include_minio and self.MINIO_ENDPOINT:
            storage_port = _endpoint_port(self.MINIO_ENDPOINT)
            forwards = {port: upstream for port, upstream in forwards.items()
                        if port != storage_port and upstream != self.MINIO_ENDPOINT}
        return sorted(forwards.items())

    @property
    def files_cutover_active(self) -> bool:
        """管理 API 持久化的切换状态；Hub 重启也不恢复 v1 或 MinIO 转发。"""
        return (Path(self.WORK_AREA_ROOT) / '.files-cutover.json').is_file()

    def minio_endpoint_for_container(self) -> str:
        """容器内访问 MinIO 的地址：网络策略可用时经网关主机名（两张网都通），否则原值。"""
        if self.MINIO_ENDPOINT and self.network_policy_supported:
            return f"{self.SANDBOX_GATEWAY_NAME}:{_endpoint_port(self.MINIO_ENDPOINT)}"
        return self.MINIO_ENDPOINT

    @property
    def expected_app_version(self) -> str:
        """仓库声明的沙盒 app 版本（images/ubuntu/app/VERSION）。

        容器 app 经 /api/system/health 报告自身版本（镜像构建时 COPY 同一文件），
        两者不一致即「代码已合、镜像未重建」的部署漂移（issue #6），由 reconciler
        周期对账并告警。读不到（非源码部署）返回空串 = 关闭对账。
        """
        path = REPO_ROOT / "images" / "ubuntu" / "app" / "VERSION"
        try:
            return path.read_text().strip()
        except OSError:
            return ""

    @property
    def mount_ready(self) -> bool:
        """是否具备工作区挂载能力：总开关开启且 MinIO 凭据齐全。"""
        return bool(
            self.WORKSPACE_MOUNT_ENABLED
            and self.MINIO_ENDPOINT
            and self.MINIO_ACCESS_KEY
            and self.MINIO_SECRET_KEY
        )

    def minio_rclone_env(self, *, from_host: bool = False) -> dict[str, str]:
        """供 rclone 用的 S3(MinIO) remote 配置（经 RCLONE_CONFIG_* 环境变量内联，
        无需落 rclone.conf）。remote 名固定为 ``minio``。

        ``from_host=True``：给 host 网络的 cr-fs 用，直接用 MINIO_ENDPOINT 原值（宿主可达地址）；
        否则给 v1 容器内 rclone 用，经网关主机名访问。"""
        scheme = "https" if self.MINIO_SECURE else "http"
        endpoint = self.MINIO_ENDPOINT if from_host else self.minio_endpoint_for_container()
        return {
            "RCLONE_CONFIG_MINIO_TYPE": "s3",
            "RCLONE_CONFIG_MINIO_PROVIDER": "Minio",
            "RCLONE_CONFIG_MINIO_ENV_AUTH": "false",
            "RCLONE_CONFIG_MINIO_ACCESS_KEY_ID": self.MINIO_ACCESS_KEY,
            "RCLONE_CONFIG_MINIO_SECRET_ACCESS_KEY": self.MINIO_SECRET_KEY,
            "RCLONE_CONFIG_MINIO_ENDPOINT": f"{scheme}://{endpoint}",
            # MinIO 用 path-style 寻址，且固定 region 占位避免签名差异。
            "RCLONE_CONFIG_MINIO_FORCE_PATH_STYLE": "true",
            "RCLONE_CONFIG_MINIO_REGION": "us-east-1",
        }

    # ── v2：按工作区分配的执行实例（ADR 0025）───────────────────────────────

    @property
    def work_area_root(self) -> Path:
        return Path(self.WORK_AREA_ROOT) if self.WORK_AREA_ROOT else REPO_ROOT / "var" / "sandboxhub"

    @property
    def wa_network(self) -> str:
        """v2 联网态网络：关闭 ICC（实例之间互不可达），netguard 只管这两张网。"""
        return f"{self.SANDBOX_NETWORK}-wa"

    @property
    def wa_network_isolated(self) -> str:
        return f"{self.SANDBOX_NETWORK_ISOLATED}-wa"

    def wa_network_for_mode(self, mode: str) -> str:
        return self.wa_network_isolated if mode == "deny" else self.wa_network

    @property
    def wa_gateway_name(self) -> str:
        """v2 网关容器名；在 v2 网络上带别名 SANDBOX_GATEWAY_NAME，实例内主机名与 v1 一致。"""
        return f"{self.SANDBOX_GATEWAY_NAME}-wa"

    @property
    def fs_sidecar_name(self) -> str:
        return f"{self.SANDBOX_GATEWAY_NAME}-fs"

    def image_v2_for_type(self, sandbox_type: str) -> str:
        mapping = {"ubuntu": self.DOCKER_IMAGE_UBUNTU_V2, "code": self.DOCKER_IMAGE_CODE_V2}
        if sandbox_type not in mapping:
            raise ValueError(f"未知 sandbox_type: {sandbox_type}")
        return mapping[sandbox_type]

    @property
    def hub_bound_to_loopback(self) -> bool:
        return self.SANDBOX_HUB_HOST in {"127.0.0.1", "::1", "localhost"}

    def netguard_host_allow_ports(self) -> list[int]:
        """实例可直连的宿主端口：显式白名单 + 指向宿主的 HTTP 代理端口。坏条目告警跳过。"""
        ports: set[int] = set()
        for raw in self.NETGUARD_HOST_ALLOW_PORTS.split(","):
            raw = raw.strip()
            if not raw:
                continue
            if raw.isdigit() and 0 < int(raw) < 65536:
                ports.add(int(raw))
            else:
                logger.warning("NETGUARD_HOST_ALLOW_PORTS 条目非法，跳过：{!r}", raw)
        proxy = self.SANDBOX_HTTP_PROXY.strip()
        if proxy:
            parts = urlsplit(proxy)
            if parts.hostname == "host.docker.internal" and parts.port:
                ports.add(parts.port)
        return sorted(ports)

    def netguard_egress_allow_cidrs(self) -> list[str]:
        cidrs: list[str] = []
        for raw in self.NETGUARD_EGRESS_ALLOW_CIDRS.split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                cidrs.append(str(ipaddress.ip_network(raw, strict=False)))
            except ValueError:
                logger.warning("NETGUARD_EGRESS_ALLOW_CIDRS 条目非法，跳过：{!r}", raw)
        return cidrs


def load_settings(
    *,
    system_yaml: Path = SYSTEM_YAML_PATH,
    env_file: str | Path | None = ".env",
    **overrides: Any,
) -> Settings:
    """构造 Settings：``system_yaml`` 指定系统调优文件，``env_file`` 指定 dotenv（None=不读），
    ``overrides`` 为显式字段值（最高优先级，测试用）。"""
    token = _system_yaml_ctx.set(Path(system_yaml))
    try:
        return Settings(_env_file=env_file, **overrides)
    finally:
        _system_yaml_ctx.reset(token)


settings = Settings()
