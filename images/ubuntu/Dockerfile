# ============================================================
# 沙盒操作服务 Docker 镜像 (V4 root 版)
# 基于 Ubuntu 22.04，提供虚拟桌面环境 + FastAPI 操作接口
# 包含：全中文环境、剪贴板双向同步、僵尸进程防范、标准文件流水线
# 变更：统一 root 用户运行，移除 computeruse，技能环境全预装
# ============================================================

FROM docker.io/ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV DEBIAN_PRIORITY=high

# ===== 1. 替换 APT 源为阿里云镜像（amd64 + arm64）=====
RUN sed -i 's|http://archive.ubuntu.com/ubuntu|http://mirrors.aliyun.com/ubuntu|g' /etc/apt/sources.list && \
    sed -i 's|http://security.ubuntu.com/ubuntu|http://mirrors.aliyun.com/ubuntu|g' /etc/apt/sources.list && \
    sed -i 's|http://ports.ubuntu.com/ubuntu-ports|http://mirrors.aliyun.com/ubuntu-ports|g' /etc/apt/sources.list

# ===== 2. 基础工具、编译依赖与关键系统组件 =====
RUN apt-get update && apt-get -y upgrade && \
    apt-get -y install --no-install-recommends \
    # --- 基础工具 ---
    ca-certificates curl git sudo wget gnupg \
    # --- 僵尸进程回收 (极其重要，防止沙盒假死) ---
    dumb-init \
    # --- 虚拟桌面与 VNC (TigerVNC 替代 Xvfb + x11vnc) ---
    tigervnc-standalone-server \
    xterm xdotool scrot imagemagick \
    openbox \
    # --- 剪贴板修复 (解决 noVNC 无法复制粘贴) ---
    autocutsel xclip \
    # --- 中文环境与字体 (解决乱码和浏览器弹窗) ---
    locales \
    language-pack-zh-hans \
    fonts-noto-cjk \
    fonts-wqy-microhei \
    fonts-wqy-zenhei \
    # --- Python/pyenv 编译依赖 ---
    build-essential libssl-dev zlib1g-dev libbz2-dev \
    libreadline-dev libsqlite3-dev libncursesw5-dev \
    xz-utils tk-dev libxml2-dev libxmlsec1-dev libffi-dev liblzma-dev \
    # --- 网络工具 ---
    net-tools netcat && \
    # 生成并设置中文 locale
    locale-gen zh_CN.UTF-8 && \
    update-locale LANG=zh_CN.UTF-8 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# 设置全局环境变量为中文
ENV LANG=zh_CN.UTF-8
ENV LANGUAGE=zh_CN:zh
ENV LC_ALL=zh_CN.UTF-8

# ===== 3. 安装桌面应用程序 (LibreOffice 等，无 Firefox) =====
RUN apt-get update && \
    apt-get -y install --no-install-recommends \
    libreoffice x11-apps xpdf gedit xpaint tint2 galculator pcmanfm zip unzip && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# ===== 4. 安装浏览器 (Chrome amd64 / Chromium arm64) =====
# Google Chrome 官方只提供 amd64 包；Ubuntu 22.04 的 chromium-browser 是 snap 过渡包，Docker 内不可用
# arm64 改用 Debian bookworm 的真实 Chromium .deb，并创建同名软链接保持下游代码不变
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "amd64" ]; then \
        curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
          | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg && \
        echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] \
          http://dl.google.com/linux/chrome/deb/ stable main" \
          > /etc/apt/sources.list.d/google-chrome.list && \
        apt-get update && \
        apt-get install -y --no-install-recommends google-chrome-stable; \
    else \
        curl -fsSL https://ftp-master.debian.org/keys/archive-key-12.asc \
          | gpg --dearmor -o /usr/share/keyrings/debian-bookworm.gpg && \
        echo "deb [arch=arm64 signed-by=/usr/share/keyrings/debian-bookworm.gpg] \
          http://deb.debian.org/debian bookworm main" \
          > /etc/apt/sources.list.d/debian-bookworm.list && \
        apt-get update && \
        apt-get install -y --no-install-recommends chromium && \
        ln -sf /usr/bin/chromium /usr/local/bin/google-chrome-stable && \
        rm /etc/apt/sources.list.d/debian-bookworm.list; \
    fi && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# ===== 5. 安装 Node.js 20.x (从 npmmirror CDN 国内镜像下载) =====
ENV NODE_VERSION=20.18.0
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
    echo "Node: $(node --version), npm: $(npm --version), pnpm: $(pnpm --version)"

# ===== 6. 安装 noVNC =====
RUN curl -fsSL https://github.com/novnc/noVNC/archive/refs/tags/v1.5.0.tar.gz \
      | tar -xz -C /opt && \
    mv /opt/noVNC-1.5.0 /opt/noVNC && \
    curl -fsSL https://github.com/novnc/websockify/archive/refs/tags/v0.12.0.tar.gz \
      | tar -xz -C /opt/noVNC/utils && \
    mv /opt/noVNC/utils/websockify-0.12.0 /opt/noVNC/utils/websockify && \
    ln -s /opt/noVNC/vnc.html /opt/noVNC/index.html

# ===== 7. 工作目录与沙盒工作区 =====
ENV HOME=/root
WORKDIR /root

RUN mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

# /workspace 为根级工作区，CreateRoleAPI 通过 push_to_sandbox 写入 persona/、memory/ 等文件
RUN mkdir -p /workspace/persona \
             /workspace/memory \
             /workspace/session/input \
             /workspace/session/output \
             /workspace/skills \
             /workspace/tmp

RUN mkdir -p /root/Downloads

# ===== 8. 安装 Python 环境 (Pyenv) =====
RUN git clone https://github.com/pyenv/pyenv.git /root/.pyenv && \
    cd /root/.pyenv && src/configure && make -C src && cd .. && \
    echo 'export PYENV_ROOT="/root/.pyenv"' >> /root/.bashrc && \
    echo 'command -v pyenv >/dev/null || export PATH="$PYENV_ROOT/bin:$PATH"' >> /root/.bashrc && \
    echo 'eval "$(pyenv init -)"' >> /root/.bashrc

ENV PYENV_ROOT="/root/.pyenv"
ENV PATH="/root/.pyenv/bin:$PATH"
ENV PYENV_VERSION=3.11.6

RUN eval "$(pyenv init -)" && \
    pyenv install $PYENV_VERSION && \
    pyenv global $PYENV_VERSION && \
    pyenv rehash

ENV PATH="/root/.pyenv/shims:/root/.pyenv/bin:$PATH"

# ===== 9. Python 依赖安装 =====
RUN python -m pip install --upgrade pip==23.1.2 setuptools==58.0.4 wheel==0.40.0 \
      -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple --trusted-host mirrors.aliyun.com && \
    python -m pip config set global.index-url https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple

COPY app/requirements.txt /root/computer_use_demo/requirements.txt
RUN python -m pip install -r /root/computer_use_demo/requirements.txt

# ===== 9b. Node.js Playwright（pdf skill render_cover.js 使用）=====
ENV PLAYWRIGHT_BROWSERS_PATH=/usr/local/ms-playwright
RUN npm install -g playwright && \
    PLAYWRIGHT_BROWSERS_PATH=/usr/local/ms-playwright npx playwright install chromium --with-deps && \
    chmod -R a+rx /usr/local/ms-playwright

# ===== 9c. .NET 8.0 SDK（docx skill，运行时编译）=====
RUN curl -fsSL https://dot.net/v1/dotnet-install.sh \
      | bash -s -- --channel 8.0 --install-dir /usr/local/dotnet && \
    ln -sf /usr/local/dotnet/dotnet /usr/local/bin/dotnet
ENV PATH="/usr/local/dotnet:$PATH"

# ===== 9d. 预热 NuGet 缓存（docx skill：DocumentFormat.OpenXml + System.CommandLine）=====
# root 用户 restore，缓存落在 /root/.nuget/packages/，运行时直接命中本地缓存
RUN mkdir -p /tmp/nuget-warmup && \
    printf '<Project Sdk="Microsoft.NET.Sdk">\n  <PropertyGroup>\n    <TargetFramework>net8.0</TargetFramework>\n  </PropertyGroup>\n  <ItemGroup>\n    <PackageReference Include="DocumentFormat.OpenXml" Version="3.5.1" />\n    <PackageReference Include="System.CommandLine" Version="2.0.5" />\n  </ItemGroup>\n</Project>\n' \
      > /tmp/nuget-warmup/warmup.csproj && \
    dotnet restore /tmp/nuget-warmup/warmup.csproj && \
    rm -rf /tmp/nuget-warmup

# ===== 10. 代码与入口配置 =====
COPY scripts/ /root/
COPY app/ /root/computer_use_demo/
RUN chmod +x /root/entrypoint.sh

ARG DISPLAY_NUM=1
ARG HEIGHT=768
ARG WIDTH=1024
ENV DISPLAY_NUM=$DISPLAY_NUM
ENV HEIGHT=$HEIGHT
ENV WIDTH=$WIDTH

EXPOSE 8000 8001 6080 5900

# 核心优化：使用 dumb-init 包裹 entrypoint，接管 PID 1
ENTRYPOINT [ "/usr/bin/dumb-init", "--", "./entrypoint.sh" ]
