"""
FastAPI 主入口模块 - 沙盒操作服务。

本模块是 FastAPI 应用的主入口，负责：
- 创建 FastAPI 应用实例
- 按 SANDBOX_PROFILE 注册路由（code=纯终端/文件，desktop=终端/文件+GUI）
- 配置 CORS 中间件
- 管理应用生命周期（启动/关闭时初始化/清理工具实例）

SANDBOX_PROFILE:
- code（轻量镜像）：仅 terminal/file/system/process，不加载 GUI 与其依赖（playwright/X 等）
- desktop（桌面镜像）：在 code 基础上增加 screen/mouse/keyboard/browser/CDP
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import APP_VERSION
from .routers import (
    file as file_router,
    process as process_router,
    system as system_router,
    terminal as terminal_router,
)
from .tools import BashTool, EditTool

PROFILE = os.getenv("SANDBOX_PROFILE", "desktop")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理：启动时初始化工具实例并注入路由，关闭时清理。"""
    print(f"正在初始化沙盒操作工具... (profile={PROFILE})")

    bash = BashTool()
    edit = EditTool()
    terminal_router.bash_tool = bash
    file_router.edit_tool = edit

    if PROFILE == "desktop":
        # GUI 工具与其依赖仅桌面 profile 加载，避免轻量镜像引入 X/playwright
        from .tools import ComputerTool
        from .routers import (
            browser as browser_router,
            keyboard as keyboard_router,
            mouse as mouse_router,
            screen as screen_router,
        )

        computer = ComputerTool()
        screen_router.computer_tool = computer
        mouse_router.computer_tool = computer
        keyboard_router.computer_tool = computer
        system_router.computer_tool = computer
        browser_router.computer_tool = computer
        process_router.computer_tool = computer

    print("沙盒操作工具初始化完成")

    yield

    print("正在关闭沙盒操作服务...")
    if bash._session is not None:
        bash._session.stop()


# 创建 FastAPI 应用实例
app = FastAPI(
    title="沙盒操作服务",
    description=(
        "基于 Docker 沙盒环境的操作接口服务。\n\n"
        "终端/文件/系统/进程接口始终可用；屏幕/鼠标/键盘/浏览器接口仅 desktop profile 提供。"
    ),
    version="2.0.0",
    lifespan=lifespan,
)

# 配置 CORS 中间件（允许所有来源访问）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # 允许所有来源
    allow_credentials=False,    # W3C 规范禁止 allow_origins=["*"] 与 credentials=True 并存
    allow_methods=["*"],        # 允许所有 HTTP 方法
    allow_headers=["*"],        # 允许所有请求头
)

# 始终注册的核心路由
app.include_router(terminal_router.router)
app.include_router(file_router.router)
app.include_router(system_router.router)
app.include_router(process_router.router)

# GUI 路由仅 desktop profile 注册
if PROFILE == "desktop":
    from .routers import (
        browser as browser_router,
        browser_cdp as browser_cdp_router,
        keyboard as keyboard_router,
        mouse as mouse_router,
        screen as screen_router,
    )

    app.include_router(screen_router.router)
    app.include_router(mouse_router.router)
    app.include_router(keyboard_router.router)
    app.include_router(browser_router.router)
    app.include_router(browser_cdp_router.router)


@app.get("/", summary="服务根路径", tags=["默认"])
async def root():
    """服务根路径，返回基本信息和当前 profile 下可用接口列表。"""
    endpoints = {
        "terminal": "/api/terminal",
        "file": "/api/file",
        "system": "/api/system",
        "process": "/api/process",
    }
    if PROFILE == "desktop":
        endpoints.update({
            "screen": "/api/screen",
            "mouse": "/api/mouse",
            "keyboard": "/api/keyboard",
            "browser": "/api/browser",
            "browser_cdp": "/api/browser/cdp",
            "window": "/api/window",
        })
    return {
        "service": "沙盒操作服务",
        "version": "2.0.0",
        "app_version": APP_VERSION,
        "profile": PROFILE,
        "docs": "/docs",
        "endpoints": endpoints,
    }
