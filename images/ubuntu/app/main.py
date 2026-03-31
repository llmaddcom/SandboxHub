"""
FastAPI 主入口模块 - 沙盒操作服务。

本模块是 FastAPI 应用的主入口，负责：
- 创建 FastAPI 应用实例
- 注册所有路由模块
- 配置 CORS 中间件
- 管理应用生命周期（启动/关闭时初始化/清理工具实例）
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routers import (
    browser as browser_router,
    browser_cdp as browser_cdp_router,
    file as file_router,
    keyboard as keyboard_router,
    mouse as mouse_router,
    process as process_router,
    screen as screen_router,
    system as system_router,
    terminal as terminal_router,
)
from .tools import BashTool, ComputerTool, EditTool


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理。

    在应用启动时初始化所有工具实例，
    在应用关闭时进行清理。

    参数:
        app: FastAPI 应用实例
    """
    # ===== 启动阶段：初始化工具实例 =====
    print("正在初始化沙盒操作工具...")

    # 初始化电脑操作工具（鼠标、键盘、屏幕）
    computer = ComputerTool()

    # 初始化终端工具
    bash = BashTool()

    # 初始化文件编辑工具
    edit = EditTool()

    # 将工具实例注入到各路由模块
    screen_router.computer_tool = computer
    mouse_router.computer_tool = computer
    keyboard_router.computer_tool = computer
    system_router.computer_tool = computer
    browser_router.computer_tool = computer
    process_router.computer_tool = computer
    terminal_router.bash_tool = bash
    file_router.edit_tool = edit

    print("沙盒操作工具初始化完成")
    print("可用接口路由:")
    print("   - /api/terminal      (终端操作)")
    print("   - /api/screen        (屏幕操作)")
    print("   - /api/mouse         (鼠标操作)")
    print("   - /api/keyboard      (键盘操作)")
    print("   - /api/file          (文件操作)")
    print("   - /api/system        (系统操作)")
    print("   - /api/browser       (浏览器操作)")
    print("   - /api/browser/cdp   (CDP 浏览器精控)")
    print("   - /api/process       (进程管理)")
    print("   - /api/window        (窗口管理)")
    print("   - /docs              (API 文档)")

    yield

    # ===== 关闭阶段：清理资源 =====
    print("正在关闭沙盒操作服务...")
    if bash._session is not None:
        bash._session.stop()


# 创建 FastAPI 应用实例
app = FastAPI(
    title="沙盒操作服务",
    description=(
        "基于 Docker 沙盒环境的操作接口服务。\n\n"
        "提供对虚拟桌面环境的完整操作能力，包括：\n"
        "- **终端操作**: 执行 bash 命令、管理终端会话（支持可配置超时）\n"
        "- **屏幕操作**: 截图、获取光标位置、区域缩放\n"
        "- **鼠标操作**: 点击、移动、拖拽、滚动\n"
        "- **键盘操作**: 按键、文本输入、长按\n"
        "- **文件操作**: 查看、创建、编辑文件\n"
        "- **系统操作**: 健康检查、等待截图、剪贴板、系统信息\n"
        "- **浏览器操作**: 打开 URL、关闭浏览器、获取活动窗口\n"
        "- **CDP 精控**: 导航、获取文本、点击、填写、执行 JS（需 Chrome 开启 CDP）\n"
        "- **进程管理**: 进程列表、终止进程、窗口管理"
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

# 注册所有路由模块
app.include_router(terminal_router.router)
app.include_router(screen_router.router)
app.include_router(mouse_router.router)
app.include_router(keyboard_router.router)
app.include_router(file_router.router)
app.include_router(system_router.router)
app.include_router(browser_router.router)
app.include_router(browser_cdp_router.router)
app.include_router(process_router.router)


@app.get("/", summary="服务根路径", tags=["默认"])
async def root():
    """服务根路径，返回基本信息和可用接口列表。"""
    return {
        "service": "沙盒操作服务",
        "version": "2.0.0",
        "docs": "/docs",
        "endpoints": {
            "terminal": "/api/terminal",
            "screen": "/api/screen",
            "mouse": "/api/mouse",
            "keyboard": "/api/keyboard",
            "file": "/api/file",
            "system": "/api/system",
            "browser": "/api/browser",
            "browser_cdp": "/api/browser/cdp",
            "process": "/api/process",
            "window": "/api/window",
        },
    }
