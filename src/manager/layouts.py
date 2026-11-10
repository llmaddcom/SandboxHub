"""
v2 实例的目录布局（ADR 0025；布局 v2 见 #71 / createrole_com_api#433 契约 B）

按 acquire 请求里的 ``layout_version`` 选择，两版并存，同一工作区的实例不跨布局复用：

v1（``/workspace`` 只读骨架）::

    /workspace/work           rw  工作区（cwd）
    /workspace/deliverables   rw  交付目录
    /workspace/inputs         ro  授权文件（硬链接视图）
    /workspace/context        ro  平台说明文档
    /workspace/role/<name>    ro|rw  角色空间目录

v2（借 Linux 目录约定，用根目录区分归属）::

    /workspace                rw  工作区（cwd；宿主 work/ 目录，与 v1 的 /workspace/work 同一份数据）
    /workspace/deliverables   rw  交付目录（宿主 deliverables/，归档点读取，规则不变）
    /workspace/AGENTS.md      ro  平台写的盘说明书（workspace_files，单文件只读 bind）
    /workspace/CLAUDE.md      ro  同上，给只认 CLAUDE.md 的框架
    /self                     ro|rw  角色空间根（rclone 排除 .cr/）
    /self/diary               ro  日记（叠在 /self 之上的只读挂载）
    /mnt/drive                ro  授权文件（硬链接视图，机制同 v1 inputs）
    /opt/cr                   ro  平台生成文件（同 v1 context，可嵌套）
    /tmp                      rw  请求带 tmp 规则时是工作区的宿主目录 tmp/（实例回收后仍可列举、读取，
                                  按规则清理）；不带时是容器临时层，实例回收即清

v2 的工作区仍是宿主 ``work-areas/<id>/{work,deliverables}``：容器视图里工作区卷就是
``/workspace``，归档、导入、统计的 ``root`` 语义与 v1 相同，已有工作区不需要搬迁。
"""
from __future__ import annotations

from dataclasses import dataclass

from src.models import V2_ROLE_TARGETS

# 平台写入 /workspace 根的只读文件：白名单，名字之外的一律拒绝
WORKSPACE_FILES = ("AGENTS.md", "CLAUDE.md")
WORKSPACE_FILE_MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Layout:
    version: int
    cwd: str
    inputs_target: str
    context_target: str
    skills_dir: str
    tools_dir: str
    # 只读位置（写入失败时容器 app 据此给出明确原因）与平台保留区（拒绝写入）
    readonly_roots: tuple[str, ...]
    hidden_roots: tuple[str, ...]

    @property
    def role_names(self) -> frozenset[str] | None:
        """允许的角色目录名；None = 不限（v1 按名字落在 /workspace/role/<name>）。"""
        return frozenset(V2_ROLE_TARGETS) if self.version >= 2 else None

    def env(self) -> dict[str, str]:
        """注入实例的布局环境变量。v1 保持引入 v2 之前的取值，一项不增。"""
        if self.version == 1:
            return {
                "SANDBOX_CWD": self.cwd,
                "SANDBOX_WORKSPACE": self.cwd,
                "SANDBOX_LAYOUT": "work-area/v1",
                "CR_SKILLS_DIR": self.skills_dir,
            }
        return {
            "SANDBOX_CWD": self.cwd,
            "SANDBOX_WORKSPACE": self.cwd,
            "SANDBOX_LAYOUT": f"work-area/v{self.version}",
            "CR_SKILLS_DIR": self.skills_dir,
            "CR_TOOLS_DIR": self.tools_dir,
            "SANDBOX_READONLY_ROOTS": ":".join(self.readonly_roots),
            "SANDBOX_HIDDEN_ROOTS": ":".join(self.hidden_roots),
        }


V1 = Layout(
    version=1,
    cwd="/workspace/work",
    inputs_target="/workspace/inputs",
    context_target="/workspace/context",
    skills_dir="/workspace/role/skills",
    tools_dir="/workspace/context/tools",
    readonly_roots=("/workspace/inputs", "/workspace/context", "/workspace/role/diary"),
    hidden_roots=(),
)
V2 = Layout(
    version=2,
    cwd="/workspace",
    inputs_target="/mnt/drive",
    context_target="/opt/cr",
    skills_dir="/self/skills",
    tools_dir="/opt/cr/tools",
    readonly_roots=(
        "/mnt/drive",
        "/opt/cr",
        V2_ROLE_TARGETS["diary"],
        *(f"/workspace/{name}" for name in WORKSPACE_FILES),
    ),
    hidden_roots=("/self/.cr",),
)
LAYOUTS: dict[int, Layout] = {1: V1, 2: V2}
SUPPORTED_LAYOUTS: tuple[int, ...] = tuple(sorted(LAYOUTS))


def get(version: int) -> Layout:
    try:
        return LAYOUTS[version]
    except KeyError:
        raise ValueError(f"不支持的目录布局版本：{version!r}") from None
