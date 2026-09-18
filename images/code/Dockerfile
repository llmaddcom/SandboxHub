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
# - tmux/bsdutils(script)：终端 job 在 tmux 窗口里经 script 跑（PTY、可交互、可 attach），见 app/tools/bash.py
# - file/xxd：探测附件真实类型（B 端失败日志：`file` 缺失是最高频的 127）
# - antiword/catdoc(含 xls2csv/catppt)：.doc/.xls/.ppt 提文字，旧版 Office 与 WPS 的兜底
# - poppler-utils：pdftotext/pdfinfo/pdfimages/pdftoppm，agent 反复 which 找它
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget gnupg \
        git openssh-client \
        dumb-init \
        build-essential pkg-config xz-utils \
        ripgrep jq less tree vim nano zip unzip procps \
        tmux bsdutils \
        file xxd antiword catdoc poppler-utils \
        rclone fuse3 \
        locales tzdata \
        fonts-noto-cjk fonts-wqy-zenhei fonts-wqy-microhei && \
    printf 'set -g remain-on-exit on\nset -g history-limit 50000\nset -g exit-empty off\nset -g status off\n' > /etc/tmux.conf && \
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

# ===== 5c. 办公文件工具链验证（B 端 call-tool 失败史：.doc/.xls/.wps 缺 olefile/xlrd/antiword，file 缺失）=====
# 真实生成 docx/xlsx/pdf 各转换一遍（markitdown extra、pymupdf、pdftotext），并逐个确认系统命令在——
# 装了但不可用的镜像直接构建失败，防「声称预装、实际缺依赖」的漂移。
COPY code/verify-office.py /tmp/verify-office.py
RUN python /tmp/verify-office.py && rm -f /tmp/verify-office.py

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

# ===== 7a. 断网现场 pip 快速失败 =====
# B 端断网（createrole `sandbox.network_enabled=false`）时 pip install 卡到超时才报错（日志里一次 60s+），
# agent 白等一轮。收紧超时/重试，十几秒内失败，agent 立刻改用预装库。构建期安装已全部完成，不受影响。
RUN python -m pip config set global.timeout 15 && python -m pip config set global.retries 1

# ===== 8. 预装清单 /etc/sandbox/MANIFEST.md（构建期实测生成，供 agent 按需 read）=====
# agent 对「预装了什么」只能靠文档转述（createrole 的 me/SANDBOX.md 是手写摘要），摘要与
# 实物漂移时以本文件为准。内网断网交付（createrole `sandbox.network_enabled=false`）下 agent
# 装不了包，先读它选替代方案。生成失败即构建失败，防「有文件但内容空」。
RUN mkdir -p /etc/sandbox && { \
      echo "# 沙盒预装清单（构建期自动生成，镜像 sandbox-code $(tr -d '[:space:]' < /app/computer_use_demo/VERSION)）"; \
      echo; echo "本文件随镜像生成，运行时不会变；pip/npm 现装的包不在此列（\`pip list\` 看实时）。"; \
      echo; echo "## 基础环境"; echo; \
      echo "- 系统：$(. /etc/os-release && echo "$PRETTY_NAME")，root，时区 $TZ，locale $LANG"; \
      echo "- Python：$(python --version 2>&1)，pip $(pip --version | awk '{print $2}')，源 $(pip config get global.index-url)"; \
      echo "- Node：$(node --version)，npm $(npm --version)，yarn $(yarn --version)，pnpm $(pnpm --version)，源 $NPM_CONFIG_REGISTRY"; \
      echo "- playwright 浏览器：$(ls /opt/ms-playwright | tr '\n' ' ')（headless chromium，\`playwright screenshot\` 可直接用）"; \
      echo "- 中文字体：$(fc-list :lang=zh family | sort -u | tr '\n' ';' )"; \
      echo; echo "## 命令行工具"; echo; \
      for c in git curl wget rg jq tree less vim nano zip unzip xz tmux rclone gcc make pkg-config todo memory skills \
               file xxd antiword catdoc xls2csv catppt pdftotext pdfinfo pdftoppm pdfimages; do \
        if command -v "$c" >/dev/null 2>&1; then echo "- $c：已装"; else echo "- $c：未装"; fi; done; \
      echo; echo "常见但**没有预装**的工具（断网时不可用，联网时可 apt-get 安装）："; \
      for c in ffmpeg ffprobe libreoffice soffice pandoc tesseract convert; do \
        command -v "$c" >/dev/null 2>&1 && echo "- $c：已装" || echo "- $c：未装"; done; \
      echo; echo "## 文件格式 → 用什么读（断网时别 pip/apt，直接用下面的）"; echo; \
      echo "| 格式 | 首选 | 备选 |"; echo "|---|---|---|"; \
      echo "| 先看类型 | \`file <路径>\` | \`xxd <路径> \| head\`（\`D0 CF 11 E0\` 开头 = 旧版 Office/WPS 的 OLE 容器，\`PK\` 开头 = docx/xlsx/pptx/ofd 的 zip 容器）|"; \
      echo "| .docx | python-docx | \`markitdown x.docx\`（转 Markdown） |"; \
      echo "| .doc（旧版 Word / 多数 .wps） | \`antiword x.doc\` 或 \`catdoc x.doc\` | olefile 读 WordDocument 流 |"; \
      echo "| .xlsx | openpyxl / pandas | \`markitdown x.xlsx\` |"; \
      echo "| .xls（旧版 Excel / 多数 .et） | xlrd 或 \`pandas.read_excel(engine='xlrd')\` | \`xls2csv x.xls\` |"; \
      echo "| .pptx / .ppt | python-pptx / \`catppt x.ppt\` | \`markitdown x.pptx\` |"; \
      echo "| .pdf | pymupdf（\`import fitz\`）| pdfplumber、pypdf、\`pdftotext -layout x.pdf -\`；扫描件 \`pdftoppm\` 出图再看 |"; \
      echo "| .ofd（政务公文） | zipfile 解开 + lxml 读 Doc_0/Pages/*/Content.xml | — |"; \
      echo "| 带密码的 Office | msoffcrypto-tool 解密到临时文件再按上面读 | pypdf 直接 \`decrypt()\`（PDF）|"; \
      echo "| .msg（Outlook） | \`markitdown x.msg\`（olefile） | — |"; \
      echo "| 没在表里的 | 先 \`file\` 看真实类型，很多附件只是改了扩展名 | — |"; \
      echo; echo "## Python 包（pip list）"; echo; echo '```'; pip list --format=freeze; echo '```'; \
      echo; echo "## Node 全局包（npm ls -g）"; echo; echo '```'; npm ls -g --depth=0 2>/dev/null; echo '```'; \
    } > /etc/sandbox/MANIFEST.md && \
    test "$(wc -l < /etc/sandbox/MANIFEST.md)" -gt 40 && \
    grep -q "^pandas==" /etc/sandbox/MANIFEST.md && \
    grep -q "^- todo：已装" /etc/sandbox/MANIFEST.md && \
    grep -q "^- antiword：已装" /etc/sandbox/MANIFEST.md && \
    grep -q "^olefile==" /etc/sandbox/MANIFEST.md && \
    grep -q "^| .doc" /etc/sandbox/MANIFEST.md && \
    echo "MANIFEST.md: $(wc -l < /etc/sandbox/MANIFEST.md) 行"

WORKDIR /app
EXPOSE 8000
ENTRYPOINT ["/usr/bin/dumb-init", "--", "/entrypoint.sh"]
