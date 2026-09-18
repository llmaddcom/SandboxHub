#!/bin/sh
# 按 FORWARDS（listen=host:port,listen=host:port,...）为每个条目起一个 socat 转发。
# 上游主机名在每次连接时解析（host.docker.internal 由 Docker 注入 /etc/hosts）。
set -eu

if [ -z "${FORWARDS:-}" ]; then
    echo "[gateway] FORWARDS 为空，无转发，仅保持运行" >&2
    exec sleep infinity
fi

pids=""
old_ifs="$IFS"; IFS=','
for entry in $FORWARDS; do
    IFS="$old_ifs"
    entry="$(printf '%s' "$entry" | tr -d ' ')"
    [ -n "$entry" ] || continue
    listen="${entry%%=*}"
    upstream="${entry#*=}"
    echo "[gateway] :$listen -> $upstream"
    socat "TCP-LISTEN:$listen,fork,reuseaddr" "TCP:$upstream" &
    pids="$pids $!"
    IFS=','
done
IFS="$old_ifs"

# 任一转发退出即整体退出，交给 Docker restart 策略拉起
for pid in $pids; do
    wait "$pid"
done
