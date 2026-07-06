#!/usr/bin/env bash
# 构建沙盒镜像并打版本标签（issue #6：镜像版本化，替代裸 :latest）
#
# 版本来源：images/ubuntu/app/VERSION（随 app 代码 COPY 进镜像，容器经
# /api/system/health 上报，SandboxHub reconciler 据此对账部署漂移）。
# 改动 images/ 下代码时请同步 bump 该文件，并在合并后执行本脚本重建镜像。
#
# 用法：scripts/build-images.sh [code|ubuntu|all]（默认 all）
#
# 代理：ubuntu 镜像需从境外拉 Chrome/noVNC/pyenv 等（见 README「使用代理构建」）。
# 设置 BUILD_PROXY 后自动以 --network host + 代理 build-arg 构建，例：
#   BUILD_PROXY=http://127.0.0.1:8118 scripts/build-images.sh ubuntu
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/images/ubuntu/app/VERSION")"
TARGET="${1:-all}"
BUILD_PROXY="${BUILD_PROXY:-}"

if [ -z "$VERSION" ]; then
    echo "错误：images/ubuntu/app/VERSION 为空" >&2
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

echo "==> 构建沙盒镜像 | version=$VERSION | target=$TARGET | proxy=${BUILD_PROXY:-无}"

if [ "$TARGET" = "code" ] || [ "$TARGET" = "all" ]; then
    docker build "${PROXY_ARGS[@]+"${PROXY_ARGS[@]}"}" -f "$ROOT/images/code/Dockerfile" \
        -t "sandbox-code:$VERSION" -t sandbox-code:latest "$ROOT/images"
fi

if [ "$TARGET" = "ubuntu" ] || [ "$TARGET" = "all" ]; then
    docker build "${PROXY_ARGS[@]+"${PROXY_ARGS[@]}"}" -f "$ROOT/images/ubuntu/Dockerfile" \
        -t "sandbox-ubuntu:$VERSION" -t sandbox-ubuntu:latest "$ROOT/images/ubuntu"
fi

echo "==> 完成。在跑的旧镜像容器不会自动替换："
echo "    - warm 容器：重启 SandboxHub 或等 reconciler 闲置回收后由 warm pool 以新镜像重建"
echo "    - 挂载容器：release 后下一次 acquire 自动用新镜像"
echo "    - reconciler 会对版本漂移的存量容器持续告警（日志关键字：镜像版本漂移）"
