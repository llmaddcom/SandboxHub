#!/bin/bash
set -e  # Exit on error

DPI=96
RES_AND_DEPTH=${WIDTH}x${HEIGHT}x24

# 确保 X11 socket 目录存在（容器运行时 /tmp 可能是空的）
if [ ! -d /tmp/.X11-unix ]; then
    sudo mkdir -p /tmp/.X11-unix
    sudo chmod 1777 /tmp/.X11-unix
fi

# Function to check if Xvfb is already running
check_xvfb_running() {
    if [ -e /tmp/.X${DISPLAY_NUM}-lock ]; then
        return 0  # Xvfb is already running
    else
        return 1  # Xvfb is not running
    fi
}

# Function to check if Xvfb is ready
wait_for_xvfb() {
    local timeout=10
    local start_time=$(date +%s)
    while ! xdpyinfo >/dev/null 2>&1; do
        if [ $(($(date +%s) - start_time)) -gt $timeout ]; then
            echo "Xvfb failed to start within $timeout seconds" >&2
            return 1
        fi
        sleep 0.1
    done
    return 0
}

# Check if Xvfb is already running
if check_xvfb_running; then
    echo "Xvfb is already running on display ${DISPLAY}"
    exit 0
fi

# Start Xvfb（将 stderr 输出到日志以便排查）
echo "Starting Xvfb on display ${DISPLAY} with resolution ${RES_AND_DEPTH}..."
Xvfb $DISPLAY -ac -screen 0 $RES_AND_DEPTH -retro -dpi $DPI -nolisten tcp 2>/tmp/xvfb_error.log &
XVFB_PID=$!

# Wait for Xvfb to start
if wait_for_xvfb; then
    echo "Xvfb started successfully on display ${DISPLAY}"
    echo "Xvfb PID: $XVFB_PID"
else
    echo "Xvfb failed to start" >&2
    echo "--- Xvfb error log ---" >&2
    cat /tmp/xvfb_error.log >&2
    echo "--- end of log ---" >&2
    kill $XVFB_PID 2>/dev/null || true
    exit 1
fi
