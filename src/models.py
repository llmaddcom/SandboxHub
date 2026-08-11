"""
SandboxHub 共享数据模型

ContainerInfo：运行中容器的基础信息（由 ContainerManager 产出）
SandboxRecord：已分配沙盒的完整记录（由 Registry 管理）
ManagedContainer：Docker 侧受管容器的对账视图（由 ContainerManager.list_managed 产出）
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

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
    # 是否注入过调用方环境变量（issue #15/#16，值可能是租户 scoped token）。
    # 注入的 env 无法从运行中容器清除，故该容器同样不入 warm pool、释放即销毁，
    # 防止凭据泄漏给下一个租户。
    env_injected: bool = False


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
