#!/usr/bin/env bash
# 执行引擎运行时（SandboxHub#52 / createrole_com_api#160），由 images/code/Dockerfile 第 5d 步调用。
#
# 输入（Dockerfile 构建参数）：ENGINE_NODE_VERSION PI_VERSION DSH_VERSION NPM_REGISTRY ENGINE_ALLOW_SCRIPTS
# 布局：
#   /opt/cr-engines/node                 Node 24 LTS（与系统 Node 20 /usr/local/bin/node 分开）
#   /opt/cr-engines/lib/node_modules     npm install -g --prefix /opt/cr-engines
#   /opt/cr-engines/bin/{pi,dsh}         包装脚本：固定用 /opt/cr-engines/node 执行，与 PATH 无关
# 两个 CLI 不进 PATH，由平台经 /api/procs 按绝对路径启动；系统 node / npm 与其全局包不受影响。
# 构建期核对：版本与构建参数一致、node-pty 可用、DSH 的 ACP 与 pi 的 RPC 各走通一个来回。
set -euo pipefail

ROOT=/opt/cr-engines
NODE="$ROOT/node/bin/node"

fail() { echo "执行引擎安装失败：$*" >&2; exit 1; }
check() { [ "$2" = "$3" ] || fail "$1 得到 '$2'，期望 '$3'"; }

# fd：pi 的文件检索用 rg / fd（ripgrep 已在系统依赖层）
apt-get update
apt-get install -y --no-install-recommends fd-find
ln -sf /usr/bin/fdfind /usr/local/bin/fd
apt-get clean
rm -rf /var/lib/apt/lists/*

case "$(dpkg --print-architecture)" in
    amd64) NODE_ARCH=x64 ;;
    arm64) NODE_ARCH=arm64 ;;
    *) fail "不支持的架构 $(dpkg --print-architecture)" ;;
esac
mkdir -p "$ROOT/node"
curl -fsSL --retry 3 --retry-delay 5 \
    "https://cdn.npmmirror.com/binaries/node/v${ENGINE_NODE_VERSION}/node-v${ENGINE_NODE_VERSION}-linux-${NODE_ARCH}.tar.xz" \
    -o /tmp/engine-node.tar.xz
tar -xJf /tmp/engine-node.tar.xz -C "$ROOT/node" --strip-components=1
rm -f /tmp/engine-node.tar.xz
check "引擎 node --version" "$("$NODE" --version)" "v${ENGINE_NODE_VERSION}"

# npm 11 默认不执行依赖的安装脚本：负责原生模块（node-pty / koffi 等）就位的几个包显式放行
PATH="$ROOT/node/bin:$PATH" npm install -g --prefix "$ROOT" \
    --registry "$NPM_REGISTRY" --cache /tmp/engine-npm-cache \
    --no-fund --no-audit --no-update-notifier \
    --allow-scripts="$ENGINE_ALLOW_SCRIPTS" \
    "@earendil-works/pi-coding-agent@${PI_VERSION}" "@deepseek-ai/dsh@${DSH_VERSION}"
rm -rf /tmp/engine-npm-cache

# 包装脚本替换 npm 生成的软链（入口的 #!/usr/bin/env node 会在 PATH 里先撞上系统 Node 20）
wrap() {   # wrap <命令名> <包名>
    local entry
    entry="$("$NODE" -p "const b = require('$ROOT/lib/node_modules/$2/package.json').bin; typeof b === 'string' ? b : b['$1']")"
    [ -f "$ROOT/lib/node_modules/$2/$entry" ] || fail "$2 的入口 $entry 不存在"
    rm -f "$ROOT/bin/$1"
    printf '#!/bin/sh\nexec %s %s "$@"\n' "$NODE" "$ROOT/lib/node_modules/$2/$entry" > "$ROOT/bin/$1"
    chmod 0755 "$ROOT/bin/$1"
}
wrap pi @earendil-works/pi-coding-agent
wrap dsh @deepseek-ai/dsh

# ── 构建期核对（沿用镜像默认 PATH：系统 Node 20 在前，证明包装脚本不依赖 PATH）──
# 引擎运行时会在 TMPDIR 下留编译缓存、原生模块解包等临时文件：核对期间指向探针目录，随之删除，
# 不把构建期的临时产物带进镜像的 /tmp
probe="$(mktemp -d)"
mkdir -p "$probe/tmp" "$probe/dsh" "$probe/home"
export TMPDIR="$probe/tmp"
check "系统 node --version" "$(node --version)" "v${NODE_VERSION}"
check "pi --version" "$("$ROOT/bin/pi" --version)" "$PI_VERSION"
check "dsh --version" "$("$ROOT/bin/dsh" --version)" "$DSH_VERSION"

"$NODE" -e "
const pty = require('$ROOT/lib/node_modules/@deepseek-ai/dsh/node_modules/node-pty');
const term = pty.spawn('/bin/echo', ['pty-ok'], {});
let out = '';
term.onData((d) => { out += d; });
term.onExit((e) => { if (e.exitCode !== 0 || !out.includes('pty-ok')) { console.error('node-pty 不可用', e, out); process.exit(1); } });
" || fail "node-pty 不可用"

# 管道末端 head 读到首行即退出，引擎随之收到 SIGPIPE / EOF：只看回复内容，不看管道退出码
reply="$({ printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{}}}'; sleep 5; } \
    | DSH_HOME="$probe/dsh" timeout 60 "$ROOT/bin/dsh" --profile acp 2>"$probe/dsh.err" | head -n 1)" || true
case "$reply" in
    *'"id":1'*'"result"'*) echo "DSH ACP initialize：ok" ;;
    *) fail "DSH ACP initialize 无回复：$reply $(head -c 2000 "$probe/dsh.err")" ;;
esac
reply="$({ printf '%s\n' '{"type":"get_state","id":"1"}'; sleep 5; } \
    | HOME="$probe/home" timeout 60 "$ROOT/bin/pi" --mode rpc --no-session 2>"$probe/pi.err" | head -n 1)" || true
case "$reply" in
    *'"command":"get_state"'*'"success":true'*) echo "pi RPC get_state：ok" ;;
    *) fail "pi RPC get_state 无回复：$reply $(head -c 2000 "$probe/pi.err")" ;;
esac
unset TMPDIR
rm -rf "$probe"

echo "执行引擎运行时：node $("$NODE" --version)，pi $PI_VERSION，dsh $DSH_VERSION，$(du -sh "$ROOT" | cut -f1)"
