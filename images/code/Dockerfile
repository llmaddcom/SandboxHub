# ============================================================
# 轻量 code 沙箱镜像（无 GUI）
# 提供 Python + Node 双语言环境 + 常用开发/办公库 + 终端/文件/系统/进程接口
# 目标：秒级冷启动、国内镜像源直连、适合跑编码 Agent（参考 opencode / codex）与日常办公脚本
# 构建上下文 = SandboxHub/images（复用 ubuntu/app 同一套 app 代码）
#   docker build -f images/code/Dockerfile -t sandbox-code:latest images
# ============================================================
FROM docker.io/python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SANDBOX_PROFILE=code \
    HOME=/root \
    # slim 镜像无 locale 包，C.UTF-8 内置且可正确处理 UTF-8（中文文件名/内容）
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    NODE_VERSION=20.18.0 \
    NPM_CONFIG_REGISTRY=https://registry.npmmirror.com \
    PATH=/usr/local/bin:$PATH

# ===== 1. APT 源替换为清华镜像（Debian bookworm：deb822 与旧式 list 双兼容）=====
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g; s|security.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
            /etc/apt/sources.list.d/debian.sources; \
    fi && \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g; s|security.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
            /etc/apt/sources.list; \
    fi

# ===== 2. 系统依赖：基础工具 + 编译链 + 开发 CLI + FUSE 挂载 + 中文字体 =====
# - dumb-init：PID 1 回收僵尸进程，防沙盒假死
# - build-essential/pkg-config：供 agent 运行时 pip/npm 安装需编译的包
# - git/curl/wget/openssh-client：拉取代码与远程交互（编码 Agent 必备）
# - ripgrep/jq/less/tree/vim/nano/zip/unzip：日常检索与编辑（codex/opencode 重度依赖 rg）
# - rclone/fuse3：工作区 MinIO 挂载（容器内 rclone mount 需 fusermount3 与 /dev/fuse）
# - fonts-wqy-zenhei：matplotlib/reportlab 生成图表/PDF 时的中文字体（避免方块乱码）
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget gnupg \
        git openssh-client \
        dumb-init \
        build-essential pkg-config xz-utils \
        ripgrep jq less tree vim nano zip unzip procps \
        rclone fuse3 \
        fonts-wqy-zenhei && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# ===== 3. Node.js 20.x（从 npmmirror CDN 国内镜像下载，多架构）=====
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "amd64" ]; then NODE_ARCH="x64"; \
    elif [ "$ARCH" = "arm64" ]; then NODE_ARCH="arm64"; \
    else NODE_ARCH="$ARCH"; fi && \
    curl -fsSL --retry 3 --retry-delay 5 \
      "https://cdn.npmmirror.com/binaries/node/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz" \
      -o /tmp/node.tar.xz && \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 && \
    rm -f /tmp/node.tar.xz && \
    npm config set registry https://registry.npmmirror.com && \
    npm install -g yarn pnpm pptxgenjs && \
    yarn config set registry https://registry.npmmirror.com && \
    pnpm config set registry https://registry.npmmirror.com && \
    echo "Node: $(node --version), npm: $(npm --version), pnpm: $(pnpm --version), yarn: $(yarn --version)"

# ===== 4. pip 源替换为清华镜像（构建期安装 + 运行时 pip install 均生效）=====
RUN python -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple && \
    python -m pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn && \
    python -m pip install --upgrade pip setuptools wheel

# ===== 5. Python 依赖：基础运行依赖 + 日常办公/数据处理库（无 GUI/浏览器依赖）=====
# 先 COPY 需求文件单独成层，最大化构建缓存命中
COPY ubuntu/app/requirements-base.txt /app/requirements-base.txt
COPY ubuntu/app/requirements-code.txt /app/requirements-code.txt
RUN python -m pip install -r /app/requirements-code.txt

# ===== 6. 复用与 desktop 同一套 app 代码（profile=code 时不加载 GUI 路由/依赖）=====
COPY ubuntu/app/ /app/computer_use_demo/
COPY code/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /workspace

WORKDIR /app
EXPOSE 8000
ENTRYPOINT ["/usr/bin/dumb-init", "--", "/entrypoint.sh"]
