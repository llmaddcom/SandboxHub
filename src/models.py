"""
SandboxHub 共享数据模型

ContainerInfo：运行中容器的基础信息（由 ContainerManager 产出）
SandboxRecord：已分配沙盒的完整记录（由 Registry 管理）
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

SandboxType = Literal["ubuntu", "code"]
SandboxStatus = Literal["ready", "released"]


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


@dataclass
class SandboxRecord:
    sandbox_id: str
    container_info: ContainerInfo
    user_id: str
    role_id: str
    status: SandboxStatus
    acquired_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
