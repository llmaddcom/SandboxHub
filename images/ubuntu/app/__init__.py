"""
沙盒操作服务包 - 基于 Docker 沙盒环境的 FastAPI 操作接口。

本包提供了对虚拟桌面沙盒环境的完整操作能力，
通过 FastAPI 将各种操作封装为 RESTful 接口。
"""

from pathlib import Path

# 容器内 app 版本，来源于随代码一起 COPY 进镜像的 VERSION 文件。
# 经 /api/system/health 暴露，供 SandboxHub 对账「镜像内 app 版本 vs 仓库声明版本」，
# 防止「代码已合、镜像未重建」的静默部署漂移（issue #6）。
try:
    APP_VERSION = (Path(__file__).resolve().parent / "VERSION").read_text().strip() or "unknown"
except OSError:
    APP_VERSION = "unknown"
