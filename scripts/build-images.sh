#!/usr/bin/env bash
# 构建沙盒镜像并打版本标签（issue #6：镜像版本化，替代裸 :latest）
#
# 执行镜像版本来源：images/ubuntu/app/VERSION（随 app 代码 COPY 进镜像，容器经
# /api/system/health 上报，SandboxHub reconciler 据此对账部署漂移）。
# cr-fs 使用 images/fs/VERSION，文件操作变更无需重建执行镜像。
# 改动镜像代码时同步 bump 对应 VERSION，并在合并后执行本脚本重建镜像。
#
# 用法：scripts/build-images.sh [code|ubuntu|gateway|fs|all]（默认 all）
# gateway = cr-host 网关镜像（alpine + socat，网络策略平台通道，SandboxHub#42）
# fs      = cr-fs 文件侧车镜像（rclone + fuse3 + iptables，按工作区分配的实例用，ADR 0025）
#
# NO_LATEST=1：只打版本标签、不动 :latest。线上 v1 沙盒用 :latest，切换窗口之前构建新镜像
# 必须带上它，避免在跑的 v1 warm pool 换成未经演练的镜像。
#
# 代理：ubuntu 镜像需从境外拉 Chrome/noVNC/pyenv 等（见 README「使用代理构建」）。
# 设置 BUILD_PROXY 后自动以 --network host + 代理 build-arg 构建，例：
#   BUILD_PROXY=http://127.0.0.1:8118 scripts/build-images.sh ubuntu
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/images/ubuntu/app/VERSION")"
FS_VERSION="$(tr -d '[:space:]' < "$ROOT/images/fs/VERSION")"
TARGET="${1:-all}"
BUILD_PROXY="${BUILD_PROXY:-}"
NO_LATEST="${NO_LATEST:-}"

# tags <镜像名> [版本]：输出 -t 参数（版本标签恒有；:latest 视 NO_LATEST 而定）
tags() {
    echo "-t" "$1:${2:-$VERSION}"
    if [ -z "$NO_LATEST" ]; then
        echo "-t" "$1:latest"
    fi
}

if [ -z "$VERSION" ]; then
    echo "错误：images/ubuntu/app/VERSION 为空" >&2
    exit 1
fi
if [ -z "$FS_VERSION" ]; then
    echo "错误：images/fs/VERSION 为空" >&2
    exit 1
fi

PROXY_ARGS=()
if [ -n "$BUILD_PROXY" ]; then
    PROXY_ARGS=(
        --network host
        --build-arg "HTTP_PROXY=$BUILD_PROXY"
        --build-arg "HTTPS_PROXY=$BUILD_PROXY"
        --build-arg "http_proxy=$BUILD_PROXY"
        --build-arg "https_proxy=$BUILD_PROXY"
        --build-arg "NO_PROXY=localhost,127.0.0.1,mirrors.tuna.tsinghua.edu.cn,mirrors.aliyun.com,pypi.tuna.tsinghua.edu.cn,registry.npmmirror.com,cdn.npmmirror.com"
        --build-arg "no_proxy=localhost,127.0.0.1,mirrors.tuna.tsinghua.edu.cn,mirrors.aliyun.com,pypi.tuna.tsinghua.edu.cn,registry.npmmirror.com,cdn.npmmirror.com"
    )
fi

echo "==> 构建沙盒镜像 | app_version=$VERSION | fs_version=$FS_VERSION | target=$TARGET | proxy=${BUILD_PROXY:-无} | latest=$([ -z "$NO_LATEST" ] && echo 是 || echo 否)"

if [ "$TARGET" = "code" ] || [ "$TARGET" = "all" ]; then
    # shellcheck disable=SC2046
    docker build "${PROXY_ARGS[@]+"${PROXY_ARGS[@]}"}" -f "$ROOT/images/code/Dockerfile" \
        $(tags sandbox-code) "$ROOT/images"
fi

if [ "$TARGET" = "ubuntu" ] || [ "$TARGET" = "all" ]; then
    # shellcheck disable=SC2046
    docker build "${PROXY_ARGS[@]+"${PROXY_ARGS[@]}"}" -f "$ROOT/images/ubuntu/Dockerfile" \
        $(tags sandbox-ubuntu) "$ROOT/images/ubuntu"
fi

if [ "$TARGET" = "gateway" ] || [ "$TARGET" = "all" ]; then
    # shellcheck disable=SC2046
    docker build "${PROXY_ARGS[@]+"${PROXY_ARGS[@]}"}" -f "$ROOT/images/gateway/Dockerfile" \
        $(tags sandbox-gateway) "$ROOT/images/gateway"
fi

if [ "$TARGET" = "fs" ] || [ "$TARGET" = "all" ]; then
    # shellcheck disable=SC2046
    docker build "${PROXY_ARGS[@]+"${PROXY_ARGS[@]}"}" -f "$ROOT/images/fs/Dockerfile" \
        $(tags sandbox-fs "$FS_VERSION") "$ROOT/images/fs"
fi

echo "==> 完成。在跑的旧镜像容器不会自动替换："
echo "    - warm 容器：重启 SandboxHub 或等 reconciler 闲置回收后由 warm pool 以新镜像重建"
echo "    - 挂载容器：release 后下一次 acquire 自动用新镜像"
echo "    - reconciler 会对版本漂移的存量容器持续告警（日志关键字：镜像版本漂移）"
echo "    - 网关容器 cr-host：SandboxHub 启动 / 每轮对账发现镜像或转发表变化会自动重建"
