#!/bin/bash
set -e
GEOMETRY="${WIDTH}x${HEIGHT}"

if [ ! -d /tmp/.X11-unix ]; then
    sudo mkdir -p /tmp/.X11-unix && sudo chmod 1777 /tmp/.X11-unix
fi

[ -e /tmp/.X${DISPLAY_NUM}-lock ] && echo "Xvnc already running" && exit 0

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
