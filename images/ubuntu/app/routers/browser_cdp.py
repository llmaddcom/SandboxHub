"""
CDP 浏览器精控路由模块 - 通过 Chrome DevTools Protocol 精准控制浏览器。

使用 playwright 的 connect_over_cdp 连接本地 Chrome（需在 entrypoint 启动时已开启 --remote-debugging-port=9222）。

接口列表：
- POST /api/browser/cdp/navigate          : 导航到指定 URL
- POST /api/browser/cdp/get_text          : 获取页面或元素文本
- POST /api/browser/cdp/click_selector    : 点击 CSS 选择器
- POST /api/browser/cdp/fill_input        : 填写输入框
- POST /api/browser/cdp/evaluate          : 执行 JavaScript
- GET  /api/browser/cdp/url               : 获取当前 URL 和标题
- GET  /api/browser/cdp/html              : 获取页面 HTML
- POST /api/browser/cdp/scroll            : 滚动页面或元素
- POST /api/browser/cdp/hover             : 悬停到元素
- POST /api/browser/cdp/wait_for_selector : 等待元素出现
"""

import asyncio

from fastapi import APIRouter, HTTPException
from playwright.async_api import async_playwright, Browser, Page
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/browser/cdp", tags=["CDP 浏览器精控"])

# ===== 懒初始化状态 =====
_lock: asyncio.Lock | None = None
_op_lock: asyncio.Lock | None = None
_browser: Browser | None = None
_page: Page | None = None
_playwright_ctx = None

CDP_URL = "http://localhost:9222"


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def _get_op_lock() -> asyncio.Lock:
    """返回 CDP 操作级全局锁，防止并发操作同一 Playwright Page 导致竞态。"""
    global _op_lock
    if _op_lock is None:
        _op_lock = asyncio.Lock()
    return _op_lock


async def _get_page() -> Page:
    """懒连接到本地 Chrome CDP，失败时返回 503。"""
    global _browser, _page, _playwright_ctx

    async with _get_lock():
        # 检查现有连接是否仍然有效
        if _page is not None:
            try:
                await _page.title()
                return _page
            except Exception:
                _browser = None
                _page = None

        # 建立新连接
        try:
            if _playwright_ctx is None:
                _playwright_ctx = await async_playwright().start()
            _browser = await _playwright_ctx.chromium.connect_over_cdp(CDP_URL)
            contexts = _browser.contexts
            if not contexts or not contexts[0].pages:
                raise RuntimeError("Chrome 无可用页面")
            _page = contexts[0].pages[0]
            return _page
        except Exception as e:
            _browser = None
            _page = None
            raise HTTPException(
                status_code=503,
                detail=f"无法连接 Chrome CDP ({CDP_URL})，请确认 Chrome 已启动: {e}",
            )


# ==================== 请求/响应模型 ====================

class NavigateRequest(BaseModel):
    url: str = Field(..., description="要导航到的 URL", examples=["https://example.com"])


class NavigateResponse(BaseModel):
    success: bool
    url: str
    title: str


class GetTextRequest(BaseModel):
    selector: str | None = Field(
        default=None,
        description="CSS 选择器，None 则返回整页文本",
        examples=["h1", "#main-content"],
    )


class GetTextResponse(BaseModel):
    success: bool
    text: str


class ClickRequest(BaseModel):
    selector: str = Field(..., description="要点击的 CSS 选择器", examples=["button#submit"])


class ClickResponse(BaseModel):
    success: bool
    message: str


class FillInputRequest(BaseModel):
    selector: str = Field(..., description="输入框的 CSS 选择器", examples=["input[name='q']"])
    value: str = Field(..., description="要填入的值")


class FillInputResponse(BaseModel):
    success: bool
    message: str


class EvaluateRequest(BaseModel):
    script: str = Field(..., description="要执行的 JavaScript 代码", examples=["document.title"])


class EvaluateResponse(BaseModel):
    success: bool
    result: str


class UrlResponse(BaseModel):
    success: bool
    url: str
    title: str


class HtmlResponse(BaseModel):
    success: bool
    html: str


class ScrollRequest(BaseModel):
    selector: str | None = Field(
        default=None,
        description="要滚动的元素 CSS 选择器，None 则滚动整个页面",
    )
    direction: str = Field(
        ...,
        description="滚动方向：up / down / left / right",
        examples=["down"],
    )
    amount: int = Field(
        default=300,
        description="滚动像素量",
        examples=[300],
    )


class ScrollResponse(BaseModel):
    success: bool
    message: str


class HoverRequest(BaseModel):
    selector: str = Field(..., description="要悬停的元素 CSS 选择器")


class HoverResponse(BaseModel):
    success: bool
    message: str


class WaitForSelectorRequest(BaseModel):
    selector: str = Field(..., description="等待出现的元素 CSS 选择器")
    timeout_ms: int = Field(default=5000, description="等待超时（毫秒）")
    state: str = Field(
        default="visible",
        description="等待状态：visible / hidden / attached / detached",
    )


class WaitForSelectorResponse(BaseModel):
    success: bool
    message: str


# ==================== API 接口 ====================

@router.post("/navigate", response_model=NavigateResponse, summary="导航到 URL")
async def cdp_navigate(request: NavigateRequest):
    """导航 Chrome 到指定 URL，等待 DOM 加载完成后返回页面标题。"""
    async with _get_op_lock():
        page = await _get_page()
        try:
            await page.goto(request.url, wait_until="domcontentloaded", timeout=30000)
            title = await page.title()
            return NavigateResponse(success=True, url=page.url, title=title)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"导航失败: {e}")


@router.post("/get_text", response_model=GetTextResponse, summary="获取页面或元素文本")
async def cdp_get_text(request: GetTextRequest):
    """获取整页文本内容，或指定 CSS 选择器对应元素的文本。"""
    async with _get_op_lock():
        page = await _get_page()
        try:
            if request.selector:
                element = page.locator(request.selector).first
                text = await element.inner_text(timeout=5000)
            else:
                text = await page.inner_text("body", timeout=10000)
            return GetTextResponse(success=True, text=text)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"获取文本失败: {e}")


@router.post("/click_selector", response_model=ClickResponse, summary="点击 CSS 选择器")
async def cdp_click_selector(request: ClickRequest):
    """点击指定 CSS 选择器对应的第一个元素。"""
    async with _get_op_lock():
        page = await _get_page()
        try:
            await page.locator(request.selector).first.click(timeout=5000)
            return ClickResponse(success=True, message=f"已点击 {request.selector}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"点击失败: {e}")


@router.post("/fill_input", response_model=FillInputResponse, summary="填写输入框")
async def cdp_fill_input(request: FillInputRequest):
    """清空指定输入框并填入新值。"""
    async with _get_op_lock():
        page = await _get_page()
        try:
            locator = page.locator(request.selector).first
            await locator.clear(timeout=5000)
            await locator.fill(request.value, timeout=5000)
            return FillInputResponse(success=True, message=f"已填写 {request.selector}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"填写失败: {e}")


@router.post("/evaluate", response_model=EvaluateResponse, summary="执行 JavaScript")
async def cdp_evaluate(request: EvaluateRequest):
    """在当前页面上下文中执行 JavaScript，返回结果字符串。"""
    async with _get_op_lock():
        page = await _get_page()
        try:
            result = await page.evaluate(request.script)
            return EvaluateResponse(success=True, result=str(result))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"JS 执行失败: {e}")


@router.get("/url", response_model=UrlResponse, summary="获取当前 URL 和标题")
async def cdp_get_url():
    """返回当前页面的 URL 和标题。"""
    async with _get_op_lock():
        page = await _get_page()
        try:
            title = await page.title()
            return UrlResponse(success=True, url=page.url, title=title)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"获取 URL 失败: {e}")


@router.get("/html", response_model=HtmlResponse, summary="获取页面 HTML")
async def cdp_get_html():
    """返回当前页面的完整 HTML 内容。"""
    async with _get_op_lock():
        page = await _get_page()
        try:
            html = await page.content()
            return HtmlResponse(success=True, html=html)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"获取 HTML 失败: {e}")


@router.post("/scroll", response_model=ScrollResponse, summary="滚动页面或元素")
async def cdp_scroll(request: ScrollRequest):
    """滚动整个页面或指定元素。direction 可选 up/down/left/right，amount 为像素数。"""
    async with _get_op_lock():
        page = await _get_page()
        try:
            dx, dy = 0, 0
            if request.direction == "down":
                dy = request.amount
            elif request.direction == "up":
                dy = -request.amount
            elif request.direction == "right":
                dx = request.amount
            elif request.direction == "left":
                dx = -request.amount
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"无效的滚动方向: {request.direction}，支持 up/down/left/right",
                )

            if request.selector:
                await page.locator(request.selector).first.scroll_into_view_if_needed(timeout=5000)
                await page.locator(request.selector).first.evaluate(
                    f"el => el.scrollBy({dx}, {dy})"
                )
            else:
                await page.evaluate(f"window.scrollBy({dx}, {dy})")

            return ScrollResponse(
                success=True,
                message=f"已向 {request.direction} 滚动 {request.amount}px",
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"滚动失败: {e}")


@router.post("/hover", response_model=HoverResponse, summary="悬停到元素")
async def cdp_hover(request: HoverRequest):
    """将鼠标悬停到指定 CSS 选择器对应的第一个元素上。"""
    async with _get_op_lock():
        page = await _get_page()
        try:
            await page.locator(request.selector).first.hover(timeout=5000)
            return HoverResponse(success=True, message=f"已悬停到 {request.selector}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"悬停失败: {e}")


@router.post("/wait_for_selector", response_model=WaitForSelectorResponse, summary="等待元素出现")
async def cdp_wait_for_selector(request: WaitForSelectorRequest):
    """等待指定 CSS 选择器的元素达到目标状态（visible/hidden/attached/detached）。"""
    async with _get_op_lock():
        page = await _get_page()
        try:
            await page.wait_for_selector(
                request.selector,
                timeout=request.timeout_ms,
                state=request.state,  # type: ignore[arg-type]
            )
            return WaitForSelectorResponse(
                success=True,
                message=f"元素 {request.selector} 已达到状态 {request.state}",
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"等待元素失败: {e}")
