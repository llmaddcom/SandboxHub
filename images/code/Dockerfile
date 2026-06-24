# ============================================================
# 轻量 code 沙箱镜像（无 GUI）
# 仅提供 python 环境 + 终端/文件/系统/进程接口，秒级冷启动
# 构建上下文 = SandboxHub/images（复用 ubuntu/app 同一套 app 代码）
#   docker build -f images/code/Dockerfile -t sandbox-code:latest images
# ============================================================
FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    SANDBOX_PROFILE=code

# ===== APT 源替换为清华镜像（Debian bookworm，deb822 与旧式 list 双兼容）=====
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g; s|security.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
            /etc/apt/sources.list.d/debian.sources; \
    fi && \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g; s|security.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
            /etc/apt/sources.list; \
    fi

# 基础工具 + dumb-init(PID 1 回收僵尸进程) + 编译链(供 agent 后续 pip 安装需编译的包)
# rclone + fuse3：工作区 MinIO 挂载（容器内 rclone mount 需 fusermount3 与 /dev/fuse）
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git dumb-init build-essential rclone fuse3 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# ===== pip 源替换为清华镜像（构建期安装 + 运行时 pip install 均生效）=====
RUN python -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple && \
    python -m pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn

# 仅基础运行依赖；业务 python 包后续按需添加到 requirements-base.txt 或运行时 pip install
COPY ubuntu/app/requirements-base.txt /app/requirements-base.txt
RUN python -m pip install -r /app/requirements-base.txt

# 复用与 desktop 同一套 app 代码（profile=code 时不会加载 GUI 路由/依赖）
COPY ubuntu/app/ /app/computer_use_demo/
COPY code/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /workspace

WORKDIR /app
EXPOSE 8000
ENTRYPOINT ["/usr/bin/dumb-init", "--", "/entrypoint.sh"]
