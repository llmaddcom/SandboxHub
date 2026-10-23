# ============================================================
# cr-fs：SandboxHub 的特权文件系统 sidecar（createrole ADR 0025）
# 租户容器不再需要 SYS_ADMIN、/dev/fuse 或放开 AppArmor：角色目录的 rclone 挂载、
# 沙盒网络隔离规则（iptables）和工作区目录的 root 文件操作都在这里完成。
# 以 --privileged --network host 运行，不监听任何端口；Hub 经 docker exec 调用。
#   docker build -f images/fs/Dockerfile -t sandbox-fs:latest images/fs
# ============================================================
FROM docker.io/python:3.11-slim-bookworm

RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g; s|security.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates rclone fuse3 iptables procps tini util-linux \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY fsops.py /opt/crfs/fsops.py
RUN chmod 0755 /opt/crfs/fsops.py && rclone version | head -1 && iptables --version

LABEL sandboxhub.fs="1"
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
