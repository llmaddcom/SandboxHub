"""
SandboxHub 共享数据模型

ContainerInfo：运行中容器的基础信息（由 ContainerManager 产出）
SandboxRecord：已分配沙盒的完整记录（由 Registry 管理）
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

SandboxType = Literal["ubuntu"]
SandboxStatus = Literal["ready", "released"]


@dataclass
class ContainerInfo:
    container_id: str
    container_name: str
    container_ip: str
    sandbox_type: SandboxType


@dataclass
class SandboxRecord:
    sandbox_id: str
    container_info: ContainerInfo
    user_id: str
    role_id: str
    status: SandboxStatus
    acquired_at: datetime = field(default_factory=datetime.utcnow)
