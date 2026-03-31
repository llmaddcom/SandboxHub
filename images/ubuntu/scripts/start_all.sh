#!/bin/bash
set -e

export DISPLAY=:${DISPLAY_NUM}

# 1. 启动底层虚拟显示器（TigerVNC 内置 VNC Server）
./xvnc_startup.sh

# ==========================================
# 启动 X11 剪贴板双向同步守护进程
# 解决 noVNC 网页端与沙盒内部无法 Ctrl+C/V 的问题
# ==========================================
echo "Starting autocutsel for clipboard synchronization..."
autocutsel -fork
autocutsel -selection PRIMARY -fork
echo "Clipboard synchronization started."

# 2. 启动任务栏
./tint2_startup.sh

# 3. 启动窗口管理器（openbox 替代 mutter，CPU 占用显著降低）
./openbox_startup.sh

# 不再调用 x11vnc_startup.sh，Xvnc 已内置 VNC Server
