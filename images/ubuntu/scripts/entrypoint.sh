#!/bin/bash
# ============================================================
# 容器入口脚本
# 启动虚拟桌面环境、VNC 服务、FastAPI 接口和 MCP 服务
# ============================================================

set -e

# 0. DNS 稳定性修复（运行时写入，Docker 会覆盖构建时的 resolv.conf）
echo -e "nameserver 8.8.8.8\nnameserver 1.1.1.1" | sudo tee /etc/resolv.conf > /dev/null || true

# 1. 启动虚拟桌面环境（TigerVNC、剪贴板、tint2、openbox）
./start_all.sh

# 2. 启动 noVNC（Web 端 VNC 代理）
./novnc_startup.sh

# 3. 后台启动 Chrome CDP
# --use-gl=swiftshader: 强制软件渲染（Docker 无 GPU，SwiftShader 是唯一可靠路径）
# --in-process-gpu: GPU 线程内嵌主进程，消除跨进程 IPC 故障点
# 移除 --disable-gpu + --disable-software-rasterizer（两者叠加导致无渲染后端，IPC 阻塞）
_start_chrome() {
    DISPLAY=:${DISPLAY_NUM} google-chrome-stable \
        --no-sandbox --disable-dev-shm-usage \
        --use-gl=swiftshader \
        --in-process-gpu \
        --remote-debugging-port=9222 \
        --window-size=${WIDTH},${HEIGHT} \
        --no-first-run --no-default-browser-check \
        --disable-extensions about:blank \
        >> /tmp/chromium_cdp.log 2>&1 &
    echo $!
}

echo "Starting Chrome with CDP on port 9222..."
CHROME_PID=$(_start_chrome)

# 等待 CDP 端口就绪（最多 15s）
for i in $(seq 1 15); do
    netstat -tuln 2>/dev/null | grep -q ":9222 " && break
    sleep 1
done
echo "Chrome CDP ready (or timeout after 15s)"

# Chrome 崩溃自动重启守护进程（每 5s 检查一次）
# browser_cdp.py 的懒重连机制会自动处理 Playwright 重连，无需修改 Python 代码
(
    while true; do
        sleep 5
        if ! kill -0 $CHROME_PID 2>/dev/null; then
            echo "[chrome-watchdog] $(date): Chrome crashed (PID $CHROME_PID), restarting..."
            CHROME_PID=$(_start_chrome)
            echo "[chrome-watchdog] Chrome restarted with PID $CHROME_PID"
        fi
    done
) &

# 4. 启动 FastAPI 沙盒操作接口服务（端口 8000）
echo "正在启动沙盒操作服务..."
python -m uvicorn computer_use_demo.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --log-level info \
    > /tmp/fastapi_stdout.log 2>&1 &
FASTAPI_PID=$!

# 5. 启动 FastMCP 服务（端口 8001，SSE transport）
echo "正在启动 MCP 服务..."
python -m computer_use_demo.mcp_server \
    > /tmp/mcp_stdout.log 2>&1 &
MCP_PID=$!

echo "======================================================="
echo "沙盒操作服务已就绪！"
echo "  FastAPI 接口: http://localhost:8000"
echo "  API 文档:     http://localhost:8000/docs"
echo "  MCP 服务:     http://localhost:8001/sse"
echo "  VNC 预览:     http://localhost:6080/vnc.html?autoconnect=1"
echo "  Chrome CDP:   http://localhost:9222"
echo "======================================================="

# 监听后台核心进程，任意一个崩溃则退出容器并报警，
# 配合 dumb-init 能够实现优雅的回收和重启
# 注意：Chrome 不加入 wait -n（open_browser/close_browser 会重启它）
wait -n $FASTAPI_PID $MCP_PID

# 如果 wait -n 退出了，说明某个核心服务挂了
echo "核心服务 (FastAPI 或 MCP) 已崩溃退出，容器停止。"
exit 1
