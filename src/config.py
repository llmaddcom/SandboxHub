"""
SandboxHub 配置

职责：从 .env 加载全部配置项，提供全局单例 settings。
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    SANDBOX_HUB_PORT: int = 8088
    SANDBOX_NETWORK: str = "bridge"
    DOCKER_IMAGE_UBUNTU: str = "sandbox-ubuntu:latest"
    DOCKER_IMAGE_CODE: str = "sandbox-code:latest"
    WARM_POOL_UBUNTU: int = 3
    WARM_POOL_CODE: int = 0
    CONTAINER_LABEL: str = "sandboxhub.managed"
    SANDBOX_API_PORT: int = 8000
    POOL_MAINTAIN_INTERVAL: int = 30  # 秒
    # 容器代理：空=不注入代理。容器内 127.0.0.1 不是宿主，需用 host.docker.internal
    # 或宿主在 docker 网桥上可达的地址，例：http://host.docker.internal:8118
    SANDBOX_HTTP_PROXY: str = ""
    # 容器自定义 DNS：逗号分隔，空=用 Docker 默认 DNS
    SANDBOX_DNS: str = ""

    # ── 工作区挂载（云盘 MinIO ↔ 容器互通）────────────────────────────────────
    # acquire 请求体可携带 workspace={bucket,prefix,mount_path}；开启且凭据齐全时，
    # SandboxHub 在容器内用 rclone 把 MinIO 的 bucket/prefix 实时挂到 mount_path
    # （默认 /workspace），使容器内写文件近实时落 MinIO、MinIO 文件即时可见——
    # createrole 侧据此实现「云盘原生技能」与沙盒互通。挂载容器不进 warm pool、
    # 释放即销毁（避免跨租户串台与误删 MinIO 数据）。
    WORKSPACE_MOUNT_ENABLED: bool = True
    # rclone S3 remote 指向的 MinIO 连接信息（留空=不具备挂载能力，acquire 里的
    # workspace 被忽略并记 warning，回退为不挂载容器）。
    MINIO_ENDPOINT: str = ""          # host:port，例：minio:9000（不含 scheme）
    MINIO_ACCESS_KEY: str = ""
    MINIO_SECRET_KEY: str = ""
    MINIO_SECURE: bool = False        # True=https
    # rclone VFS 写回策略：writes=仅写文件走本地缓存并在关闭后回写，平衡一致性与吞吐。
    RCLONE_VFS_CACHE_MODE: str = "writes"
    RCLONE_VFS_WRITE_BACK: str = "1s"  # 文件关闭后回写 MinIO 的延迟（越小越接近实时）
    RCLONE_DIR_CACHE_TIME: str = "2s"  # 目录列表缓存时长（影响 MinIO→容器可见延迟）
    # 挂载就绪探测：mountpoint 检测的轮询次数与间隔（秒）。
    MOUNT_READY_RETRIES: int = 20
    MOUNT_READY_INTERVAL: float = 0.5

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

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
            "RCLONE_CONFIG_MINIO_ENDPOINT": f"{scheme}://{self.MINIO_ENDPOINT}",
            # MinIO 用 path-style 寻址，且固定 region 占位避免签名差异。
            "RCLONE_CONFIG_MINIO_FORCE_PATH_STYLE": "true",
            "RCLONE_CONFIG_MINIO_REGION": "us-east-1",
        }


settings = Settings()
