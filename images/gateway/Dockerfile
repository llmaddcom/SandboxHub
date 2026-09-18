# ============================================================
# cr-host 网关镜像（网络策略平台通道，SandboxHub#42）
# 只做 TCP 端口转发：同时接在联网态网络与隔离网上，把 MinIO / createrole API 两类
# 平台通道转发到宿主；隔离网里的沙盒只能到达它，到不了宿主、局域网与公网。
# 转发表由 SandboxHub 经 FORWARDS 环境变量注入（listen=host:port,...）。
#   docker build -f images/gateway/Dockerfile -t sandbox-gateway:latest images/gateway
# ============================================================
FROM docker.io/alpine:3.20

RUN sed -i 's#dl-cdn.alpinelinux.org#mirrors.tuna.tsinghua.edu.cn#g' /etc/apk/repositories \
    && apk add --no-cache socat

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
