#!/bin/bash
# 轻量 code 镜像入口：仅启动 FastAPI 操作接口（端口 8000）
set -e

exec python -m uvicorn computer_use_demo.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --log-level info
