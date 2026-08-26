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
    # 中文环境：locale 由下方 locales 包生成（与 desktop 镜像一致），时区固定东八区
    LANG=zh_CN.UTF-8 \
    LC_ALL=zh_CN.UTF-8 \
    TZ=Asia/Shanghai \
    NODE_VERSION=20.18.0 \
    NPM_CONFIG_REGISTRY=https://registry.npmmirror.com \
    # playwright：浏览器装到固定路径（不依赖 HOME），下载走 npmmirror 国内镜像
    # （ENV 持久化，运行时 agent 补装 firefox/webkit 也走镜像源）
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright \
    # /usr/local/bin 前置遮蔽同名系统命令；$HOME/.local/bin 追加到末尾，让第三方按教程
    # 装进 ~/.local/bin 的工具可用且不遮蔽预装 CLI（issue #24，容器 HOME=/root）
    PATH=/usr/local/bin:$PATH:/root/.local/bin

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
# - fonts-noto-cjk/fonts-wqy-*：matplotlib/reportlab 生成图表/PDF 时的中文字体
#   （多套并装：Noto 覆盖全、WenQuanYi 黑体/微米黑兜底，见下方 matplotlib 默认字体配置）
# - locales/tzdata：zh_CN.UTF-8 locale 与 Asia/Shanghai 时区（国内业务时间/日期正确）
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget gnupg \
        git openssh-client \
        dumb-init \
        build-essential pkg-config xz-utils \
        ripgrep jq less tree vim nano zip unzip procps \
        rclone fuse3 \
        locales tzdata \
        fonts-noto-cjk fonts-wqy-zenhei fonts-wqy-microhei && \
    sed -i 's/^# *zh_CN.UTF-8 UTF-8/zh_CN.UTF-8 UTF-8/' /etc/locale.gen && \
    locale-gen && \
    ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    echo "Asia/Shanghai" > /etc/timezone && \
    fc-cache -f && \
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

# ===== 5. Python 依赖：基础运行依赖 + 日常办公/数据处理库 + playwright =====
# 先 COPY 需求文件单独成层，最大化构建缓存命中
COPY ubuntu/app/requirements-base.txt /app/requirements-base.txt
COPY ubuntu/app/requirements-code.txt /app/requirements-code.txt
RUN python -m pip install -r /app/requirements-code.txt

# ===== 5a. playwright headless chromium 预装 =====
# --with-deps 一并装 chromium 的系统依赖（libnss3/libatk 等）；浏览器二进制经
# PLAYWRIGHT_DOWNLOAD_HOST 从 npmmirror 下载到 PLAYWRIGHT_BROWSERS_PATH。
# 构建期真实 launch 一次验证可用（playwright 默认关闭 chromium sandbox，root 可跑）——
# 装了但起不来的镜像直接构建失败，防「声称预装、实际不可用」的漂移。
RUN apt-get update && \
    python -m playwright install --with-deps chromium && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    python -c "from playwright.sync_api import sync_playwright; \
p = sync_playwright().start(); b = p.chromium.launch(); \
print('playwright chromium:', b.version); b.close(); p.stop()"

# ===== 5b. matplotlib 中文开箱即用（issue #3）=====
# 默认 sans-serif 链路含 CJK 字体，中文图无需运行时自救；构建期预热字体缓存并用
# findfont(fallback_to_default=False) 验证 CJK 字体确实可被 matplotlib 解析——
# 字体缺失/发现不到时构建直接失败，防止「声称预装、实际豆腐块」的漂移再次发生。
RUN mkdir -p /root/.config/matplotlib && \
    printf 'font.family: sans-serif\nfont.sans-serif: Noto Sans CJK SC, Noto Sans CJK JP, WenQuanYi Zen Hei, DejaVu Sans\naxes.unicode_minus: False\n' \
      > /root/.config/matplotlib/matplotlibrc && \
    python -c "from matplotlib import font_manager as fm; \
print('CJK font:', fm.findfont(fm.FontProperties(family=['Noto Sans CJK SC', 'WenQuanYi Zen Hei']), fallback_to_default=False))"

# ===== 6. 预装单文件 CLI（issue #15/#16/#24：skills / todo / memory）=====
# code/bin/ 下的脚本装进 /usr/local/bin（去 .py 扩展名，须自带 shebang）。
# CLI 本体由上游仓维护（createrole 仓 server/infrastructure/sandbox_cli/），落地后
# 丢进该目录即随镜像预装；目录当前仅 README 占位，构建不因此失败。
COPY code/bin/ /opt/sandbox-cli/
RUN set -e; for f in /opt/sandbox-cli/*; do \
        case "$f" in *.md) continue;; esac; \
        [ -f "$f" ] || continue; \
        name="$(basename "$f")"; name="${name%.py}"; \
        install -m 0755 "$f" "/usr/local/bin/$name"; \
        echo "预装 CLI: $name"; \
    done

# ===== 7. 复用与 desktop 同一套 app 代码（profile=code 时不加载 GUI 路由/依赖）=====
COPY ubuntu/app/ /app/computer_use_demo/
COPY code/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /workspace

WORKDIR /app
EXPOSE 8000
ENTRYPOINT ["/usr/bin/dumb-init", "--", "/entrypoint.sh"]
