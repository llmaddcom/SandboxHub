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

import os
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    # 监听地址：默认 0.0.0.0。私有化部署建议收紧为 127.0.0.1 或指定内网 IP。
    SANDBOX_HUB_HOST: str = "0.0.0.0"
    # 受管容器标签（同机多实例隔离用，属本机属性）
    CONTAINER_LABEL: str = "sandboxhub.managed"

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

    def gateway_forwards(self) -> list[tuple[int, str]]:
        """网关容器的端口转发表 ``[(listen_port, "upstream_host:port")]``。

        MinIO 转发按 ``MINIO_ENDPOINT`` 自动加入（监听端口 = MinIO 端口，上游 = MINIO_ENDPOINT
        原值，宿主可达地址）；其余来自 ``SANDBOX_GATEWAY_FORWARDS``。坏条目告警并跳过。
        """
        forwards: dict[int, str] = {}
        if self.MINIO_ENDPOINT:
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
        return sorted(forwards.items())

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

    def minio_rclone_env(self) -> dict[str, str]:
        """供容器内 rclone 用的 S3(MinIO) remote 配置（经 RCLONE_CONFIG_* 环境变量内联，
        无需落 rclone.conf）。remote 名固定为 ``minio``。"""
        scheme = "https" if self.MINIO_SECURE else "http"
        return {
            "RCLONE_CONFIG_MINIO_TYPE": "s3",
            "RCLONE_CONFIG_MINIO_PROVIDER": "Minio",
            "RCLONE_CONFIG_MINIO_ENV_AUTH": "false",
            "RCLONE_CONFIG_MINIO_ACCESS_KEY_ID": self.MINIO_ACCESS_KEY,
            "RCLONE_CONFIG_MINIO_SECRET_ACCESS_KEY": self.MINIO_SECRET_KEY,
            "RCLONE_CONFIG_MINIO_ENDPOINT": f"{scheme}://{self.minio_endpoint_for_container()}",
            # MinIO 用 path-style 寻址，且固定 region 占位避免签名差异。
            "RCLONE_CONFIG_MINIO_FORCE_PATH_STYLE": "true",
            "RCLONE_CONFIG_MINIO_REGION": "us-east-1",
        }


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
