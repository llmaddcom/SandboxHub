#!/bin/bash
set -e
GEOMETRY="${WIDTH}x${HEIGHT}"

if [ ! -d /tmp/.X11-unix ]; then
    sudo mkdir -p /tmp/.X11-unix && sudo chmod 1777 /tmp/.X11-unix
fi

# 检查锁文件：若进程仍在运行则跳过，若是残留锁文件则清理后重启
if [ -e /tmp/.X${DISPLAY_NUM}-lock ]; then
    LOCK_PID=$(cat /tmp/.X${DISPLAY_NUM}-lock 2>/dev/null | tr -d ' ')
    if kill -0 "$LOCK_PID" 2>/dev/null; then
        echo "Xvnc already running (PID: $LOCK_PID)"
        exit 0
    else
        echo "Stale X lock file found (PID $LOCK_PID gone), cleaning up..."
        rm -f /tmp/.X${DISPLAY_NUM}-lock /tmp/.X11-unix/X${DISPLAY_NUM}
    fi
fi

Xvnc :${DISPLAY_NUM} \
    -geometry ${GEOMETRY} -depth 24 -dpi 96 \
    -SecurityTypes None -rfbport 5900 \
    -nolisten tcp \
    2>/tmp/xvnc_error.log &
XVNC_PID=$!

start=$(date +%s)
while ! DISPLAY=:${DISPLAY_NUM} xdpyinfo >/dev/null 2>&1; do
    [ $(($(date +%s)-start)) -gt 10 ] && cat /tmp/xvnc_error.log >&2 && exit 1
    sleep 0.1
done
echo "Xvnc started on display :${DISPLAY_NUM} (PID: ${XVNC_PID})"
