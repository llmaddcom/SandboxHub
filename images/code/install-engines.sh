#!/usr/bin/env bash
# 执行引擎运行时（SandboxHub#52 / createrole_com_api#160），由 images/code/Dockerfile 第 5d 步调用。
#
# 输入（Dockerfile 构建参数）：ENGINE_NODE_VERSION PI_VERSION DSH_VERSION CODEX_ACP_VERSION CODEX_VERSION
#                             CLAUDE_AGENT_ACP_VERSION CLAUDE_AGENT_SDK_VERSION NPM_REGISTRY ENGINE_ALLOW_SCRIPTS
# 布局：
#   /opt/cr-engines/node                 Node 24 LTS（与系统 Node 20 /usr/local/bin/node 分开）
#   /opt/cr-engines/lib/node_modules     npm install -g --prefix /opt/cr-engines
#   /opt/cr-engines/bin/{pi,dsh,codex-acp,claude-agent-acp}  包装脚本：固定用 /opt/cr-engines/node 执行，与 PATH 无关
# CLI 不进 PATH，由平台经 /api/procs 按绝对路径启动；系统 node / npm 与其全局包不受影响。
# codex-acp 用启动它的 Node（process.execPath）执行其依赖 @openai/codex 的 bin/codex.js，codex.js 再按
# optionalDependencies 装好的平台包（@openai/codex-linux-<arch>，原生二进制）启动 app-server：不单独
# 全局安装 @openai/codex（否则同一个约 450MB 的二进制装两份），只核对 codex-acp 依赖树里的版本。
# claude-agent-acp 精确锁定 @anthropic-ai/claude-agent-sdk，SDK 按 optionalDependencies（os/cpu/libc）装
# 平台包 claude-agent-sdk-linux-<arch>，内含原样发布的 Claude Code CLI；同样只核对依赖树里的版本。
# 两家原生包都没有安装脚本，ENGINE_ALLOW_SCRIPTS 不用放行。
# 构建期核对：版本与构建参数一致、node-pty 可用、DSH / codex-acp / claude-agent-acp 的 ACP 与 pi 的 RPC
# 各走通一个来回。
set -euo pipefail

ROOT=/opt/cr-engines
NODE="$ROOT/node/bin/node"

fail() { echo "执行引擎安装失败：$*" >&2; exit 1; }
check() { [ "$2" = "$3" ] || fail "$1 得到 '$2'，期望 '$3'"; }

# npm 与引擎运行时会在 TMPDIR 下留编译缓存、原生模块解包等临时文件：整个安装与核对期间指向
# 临时目录，最后删除，不把构建期的临时产物带进镜像的 /tmp
scratch="$(mktemp -d)"
mkdir -p "$scratch/tmp" "$scratch/dsh" "$scratch/home" "$scratch/codex" "$scratch/claude"
export TMPDIR="$scratch/tmp"

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
    -o "$scratch/node.tar.xz"
tar -xJf "$scratch/node.tar.xz" -C "$ROOT/node" --strip-components=1
rm -f "$scratch/node.tar.xz"
check "引擎 node --version" "$("$NODE" --version)" "v${ENGINE_NODE_VERSION}"

# npm 11 默认不执行依赖的安装脚本：负责原生模块（node-pty / koffi 等）就位的几个包显式放行
PATH="$ROOT/node/bin:$PATH" npm install -g --prefix "$ROOT" \
    --registry "$NPM_REGISTRY" --cache "$scratch/npm-cache" \
    --no-fund --no-audit --no-update-notifier \
    --allow-scripts="$ENGINE_ALLOW_SCRIPTS" \
    "@earendil-works/pi-coding-agent@${PI_VERSION}" "@deepseek-ai/dsh@${DSH_VERSION}" \
    "@agentclientprotocol/codex-acp@${CODEX_ACP_VERSION}" \
    "@agentclientprotocol/claude-agent-acp@${CLAUDE_AGENT_ACP_VERSION}"
rm -rf "$scratch/npm-cache"

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
wrap codex-acp @agentclientprotocol/codex-acp
wrap claude-agent-acp @agentclientprotocol/claude-agent-acp

# ── 构建期核对（沿用镜像默认 PATH：系统 Node 20 在前，证明包装脚本不依赖 PATH）──
check "系统 node --version" "$(node --version)" "v${NODE_VERSION}"
check "pi --version" "$("$ROOT/bin/pi" --version)" "$PI_VERSION"
check "dsh --version" "$("$ROOT/bin/dsh" --version)" "$DSH_VERSION"
check "codex-acp --version" "$("$ROOT/bin/codex-acp" --version)" "@agentclientprotocol/codex-acp $CODEX_ACP_VERSION"
CODEX_PKG="$ROOT/lib/node_modules/@agentclientprotocol/codex-acp/node_modules/@openai/codex"
check "codex-acp 依赖的 @openai/codex" "$("$NODE" -p "require('$CODEX_PKG/package.json').version")" "$CODEX_VERSION"
case "$("$NODE" "$CODEX_PKG/bin/codex.js" --version)" in
    *" $CODEX_VERSION") echo "codex 原生二进制：$CODEX_VERSION" ;;
    *) fail "codex --version 得到 '$("$NODE" "$CODEX_PKG/bin/codex.js" --version)'，期望以 $CODEX_VERSION 结尾" ;;
esac
check "claude-agent-acp --version" "$("$ROOT/bin/claude-agent-acp" --version)" "$CLAUDE_AGENT_ACP_VERSION"
CLAUDE_MODULES="$ROOT/lib/node_modules/@agentclientprotocol/claude-agent-acp/node_modules/@anthropic-ai"
check "claude-agent-acp 依赖的 claude-agent-sdk" \
    "$("$NODE" -p "require('$CLAUDE_MODULES/claude-agent-sdk/package.json').version")" "$CLAUDE_AGENT_SDK_VERSION"
check "Claude Code 平台包" \
    "$("$NODE" -p "require('$CLAUDE_MODULES/claude-agent-sdk-linux-${NODE_ARCH}/package.json').version")" "$CLAUDE_AGENT_SDK_VERSION"
claude_cli="$(HOME="$scratch/home" CLAUDE_CONFIG_DIR="$scratch/claude" DISABLE_AUTOUPDATER=1 \
    timeout 60 "$ROOT/bin/claude-agent-acp" --cli --version)" || fail "Claude Code CLI 无法运行：$claude_cli"
echo "Claude Code CLI：$claude_cli"

"$NODE" -e "
const pty = require('$ROOT/lib/node_modules/@deepseek-ai/dsh/node_modules/node-pty');
const term = pty.spawn('/bin/echo', ['pty-ok'], {});
let out = '';
term.onData((d) => { out += d; });
term.onExit((e) => { if (e.exitCode !== 0 || !out.includes('pty-ok')) { console.error('node-pty 不可用', e, out); process.exit(1); } });
" || fail "node-pty 不可用"

# Codex 管控配置（后端 createrole_com_api#383 实测）：codex-acp 把 cwd 强制标为受信项目，
# <cwd>/.codex/config.toml 会以 Project 层加载——用户数据能改 features、加
# [mcp_servers.x] command=…（启动即执行）。系统级 managed_config 优先级最高，把工作目录标为
# untrusted 即停用 Project 层；full-access 与审批 never 由后端经启动参数给出，不受影响。
# 两个工作目录都要压住：布局 v1 的 /workspace/work 与布局 v2 的 /workspace（SandboxHub#71）。
mkdir -p /etc/codex
cat > /etc/codex/managed_config.toml <<'TOML'
[projects."/workspace/work"]
trust_level = "untrusted"

[projects."/workspace"]
trust_level = "untrusted"
TOML
chmod 0444 /etc/codex/managed_config.toml
[ "$(grep -c 'trust_level = "untrusted"' /etc/codex/managed_config.toml)" = 2 ] || fail "Codex 管控配置未写入"
grep -q '^\[projects."/workspace"\]$' /etc/codex/managed_config.toml || fail "Codex 管控配置缺少 v2 工作目录"
echo "Codex 管控配置：/etc/codex/managed_config.toml"
# Claude Code 管控设置（后端 createrole_com_api#388 实测）：managed 层优先级最高，进程起来即为
# bypass，不依赖驱动随后的 set_mode，也压住 work/.claude/settings.json 的 defaultMode；关掉 auto
# 模式（每个动作多一次分类器模型请求）。项目层的 disableBypassPermissionsMode 无法由此撤销，
# 届时权限请求由后端驱动一律拒绝（工具失败，不越权）。
mkdir -p /etc/claude-code
cat > /etc/claude-code/managed-settings.json <<'JSON'
{
  "permissions": {
    "defaultMode": "bypassPermissions",
    "disableAutoMode": "disable"
  }
}
JSON
chmod 0644 /etc/claude-code/managed-settings.json
"$NODE" -e "const s=require('/etc/claude-code/managed-settings.json'); if (s.permissions.defaultMode !== 'bypassPermissions') process.exit(1)" \
    || fail "Claude Code 管控设置无效"
echo "Claude Code 管控设置：/etc/claude-code/managed-settings.json"

# 管道末端 head 读到首行即退出，引擎随之收到 SIGPIPE / EOF：只看回复内容，不看管道退出码
reply="$({ printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{}}}'; sleep 5; } \
    | DSH_HOME="$scratch/dsh" timeout 60 "$ROOT/bin/dsh" --profile acp 2>"$scratch/dsh.err" | head -n 1)" || true
case "$reply" in
    *'"id":1'*'"result"'*) echo "DSH ACP initialize：ok" ;;
    *) fail "DSH ACP initialize 无回复：$reply $(head -c 2000 "$scratch/dsh.err")" ;;
esac
reply="$({ printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{}}}'; sleep 5; } \
    | HOME="$scratch/home" CODEX_HOME="$scratch/codex" timeout 60 "$ROOT/bin/codex-acp" 2>"$scratch/codex.err" | grep -m 1 '"id":1')" || true
case "$reply" in
    *'"id":1'*'"result"'*) echo "codex-acp ACP initialize：ok" ;;
    *) fail "codex-acp ACP initialize 无回复：$reply $(head -c 2000 "$scratch/codex.err")" ;;
esac
reply="$({ printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{}}}'; sleep 5; } \
    | HOME="$scratch/home" CLAUDE_CONFIG_DIR="$scratch/claude" DISABLE_AUTOUPDATER=1 \
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 timeout 60 "$ROOT/bin/claude-agent-acp" 2>"$scratch/claude.err" \
    | grep -m 1 '"id":1')" || true
case "$reply" in
    *'"id":1'*'"result"'*) echo "claude-agent-acp ACP initialize：ok" ;;
    *) fail "claude-agent-acp ACP initialize 无回复：$reply $(head -c 2000 "$scratch/claude.err")" ;;
esac
reply="$({ printf '%s\n' '{"type":"get_state","id":"1"}'; sleep 5; } \
    | HOME="$scratch/home" timeout 60 "$ROOT/bin/pi" --mode rpc --no-session 2>"$scratch/pi.err" | head -n 1)" || true
case "$reply" in
    *'"command":"get_state"'*'"success":true'*) echo "pi RPC get_state：ok" ;;
    *) fail "pi RPC get_state 无回复：$reply $(head -c 2000 "$scratch/pi.err")" ;;
esac
unset TMPDIR
rm -rf "$scratch"

echo "执行引擎运行时：node $("$NODE" --version)，pi $PI_VERSION，dsh $DSH_VERSION，codex-acp $CODEX_ACP_VERSION（codex $CODEX_VERSION），claude-agent-acp $CLAUDE_AGENT_ACP_VERSION（claude-agent-sdk $CLAUDE_AGENT_SDK_VERSION），$(du -sh "$ROOT" | cut -f1)"
du -sh "$ROOT/lib/node_modules/@agentclientprotocol/codex-acp" "$ROOT/lib/node_modules/@agentclientprotocol/claude-agent-acp" "$ROOT/lib/node_modules/@earendil-works" "$ROOT/lib/node_modules/@deepseek-ai" || true
ls "$CLAUDE_MODULES"
