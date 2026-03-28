"""
SandboxHub 配置

职责：从 .env 加载全部配置项，提供全局单例 settings。
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    SANDBOX_HUB_PORT: int = 8088
    SANDBOX_NETWORK: str = "bridge"
    DOCKER_IMAGE_UBUNTU: str = "sandbox-ubuntu:latest"
    WARM_POOL_UBUNTU: int = 3
    CONTAINER_LABEL: str = "sandboxhub.managed"
    SANDBOX_API_PORT: int = 8000
    POOL_MAINTAIN_INTERVAL: int = 30  # 秒
    SANDBOX_HTTP_PROXY: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    def image_for_type(self, sandbox_type: str) -> str:
        mapping = {"ubuntu": self.DOCKER_IMAGE_UBUNTU}
        if sandbox_type not in mapping:
            raise ValueError(f"未知 sandbox_type: {sandbox_type}")
        return mapping[sandbox_type]

    def pool_size_for_type(self, sandbox_type: str) -> int:
        mapping = {"ubuntu": self.WARM_POOL_UBUNTU}
        return mapping.get(sandbox_type, 0)


settings = Settings()
