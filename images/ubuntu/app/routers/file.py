"""
文件操作路由模块 - 提供文件查看、创建和编辑的 API 接口。

接口列表：
- POST /api/file/view: 查看文件/目录内容
- POST /api/file/create: 创建新文件
- POST /api/file/replace: 字符串替换编辑
- POST /api/file/insert: 在指定行插入内容
"""

import os
import errno
import tempfile
from contextlib import suppress
from pathlib import Path

import anyio
from fastapi import APIRouter, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse as FastAPIFileResponse
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.formparsers import MultiPartException, MultiPartParser

from ..tools import EditTool, MatchError, ToolError
from ..tools.base import filesystem_error_detail

# Hub 从 system.yaml 注入；直接运行容器同样默认不限业务文件大小。
MAX_UPLOAD_SIZE = max(0, int(os.environ.get("SANDBOX_FILE_UPLOAD_MAX_BYTES", "0")))
_UPLOAD_CHUNK_BYTES = 1024 * 1024

# 创建文件操作路由，设置前缀和标签
router = APIRouter(prefix="/api/file", tags=["文件操作"])

# 全局文件编辑工具实例（在应用启动时初始化）
edit_tool: EditTool | None = None


def get_edit_tool() -> EditTool:
    """获取文件编辑工具实例。

    返回:
        EditTool: 文件编辑工具实例

    抛出:
        HTTPException: 如果工具未初始化
    """
    global edit_tool
    if edit_tool is None:
        edit_tool = EditTool()
    return edit_tool


# ==================== 请求/响应模型 ====================

class ViewRequest(BaseModel):
    """查看文件请求模型。"""
    path: str = Field(..., description="文件或目录的绝对路径", examples=["/home/user/test.py", "/tmp"])
    view_range: list[int] | None = Field(
        default=None,
        description="查看的行范围 [起始行, 结束行]，仅对文件有效",
        examples=[[1, 50]],
    )
    max_chars: int = Field(default=16000, ge=256, le=1000000, description="本页正文预算，不限制文件大小")
    column: int = Field(default=0, ge=0, description="起始行内的字符偏移，用于续读超长单行")


class CreateRequest(BaseModel):
    """创建文件请求模型。"""
    path: str = Field(..., description="新文件的绝对路径", examples=["/home/user/new_file.py"])
    file_text: str = Field(..., description="文件内容")


class ReplaceRequest(BaseModel):
    """字符串替换请求模型。"""
    path: str = Field(..., description="文件的绝对路径", examples=["/home/user/test.py"])
    old_str: str = Field(..., description="要替换的原始字符串")
    new_str: str | None = Field(default=None, description="替换后的新字符串（为空则删除原字符串）")
    replace_all: bool = Field(
        default=False,
        description="是否替换全部匹配（默认仅替换唯一匹配；为 false 时命中多处会返回 400）",
    )
    context: str | None = Field(
        default=None,
        description=(
            "可选定位提示（如函数/类名所在行，对应补丁语言的 @@ 头）。"
            "old_str 命中多处时优先取 context 行之后的第一处，用于消歧 not_unique"
        ),
        examples=["def main():"],
    )


class InsertRequest(BaseModel):
    """行插入请求模型。"""
    path: str = Field(..., description="文件的绝对路径", examples=["/home/user/test.py"])
    insert_line: int = Field(..., ge=0, description="插入位置的行号")
    insert_text: str = Field(..., description="要插入的文本内容")


class FileResponse(BaseModel):
    """文件操作响应模型。"""
    success: bool = Field(description="是否操作成功")
    output: str | None = Field(default=None, description="操作输出信息")
    error: str | None = Field(default=None, description="错误信息")


class FileViewResponse(FileResponse):
    path: str
    next_offset: int | None = None
    next_column: int = 0
    truncated: bool = False
    total_lines: int | None = None


# ==================== API 接口 ====================

@router.post("/view", response_model=FileViewResponse, summary="查看文件/目录内容")
async def view_file(request: ViewRequest):
    """查看文件或目录的内容。

    对于文件，返回带行号的文件内容，可选指定行范围。
    对于目录，返回目录下最多 2 层深度的文件列表。

    参数:
        request: 包含路径和可选行范围的请求体

    返回:
        FileResponse: 包含文件/目录内容
    """
    try:
        tool = get_edit_tool()
        result = await tool.view(
            request.path, request.view_range, max_chars=request.max_chars, column=request.column
        )
        return FileViewResponse(
            success=True,
            output=result.output,
            error=result.error,
            path=request.path,
            next_offset=getattr(result, "next_offset", None),
            next_column=getattr(result, "next_column", 0),
            truncated=getattr(result, "truncated", False),
            total_lines=getattr(result, "total_lines", None),
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"查看文件失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查看文件失败: {str(e)}")


@router.post("/write", response_model=FileResponse, summary="创建或覆盖文件")
async def write_file(request: CreateRequest):
    """创建或覆盖文件（upsert 语义）。

    在指定路径写入文件内容，文件不存在则创建，已存在则覆盖。
    自动创建不存在的父目录。

    参数:
        request: 包含文件路径和内容的请求体

    返回:
        FileResponse: 包含写入结果信息
    """
    try:
        tool = get_edit_tool()
        result = await tool.write(request.path, request.file_text)
        return FileResponse(
            success=True,
            output=result.output,
            error=result.error,
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"写入文件失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"写入文件失败: {str(e)}")


@router.post("/create", response_model=FileResponse, summary="创建新文件")
async def create_file(request: CreateRequest):
    """创建一个新文件。

    在指定路径创建新文件并写入内容。
    如果文件已存在，操作将失败。

    参数:
        request: 包含文件路径和内容的请求体

    返回:
        FileResponse: 包含创建结果信息
    """
    try:
        tool = get_edit_tool()
        result = await tool.create(request.path, request.file_text)
        return FileResponse(
            success=True,
            output=result.output,
            error=result.error,
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"创建文件失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建文件失败: {str(e)}")


@router.post("/replace", response_model=FileResponse, summary="字符串替换编辑")
async def replace_in_file(request: ReplaceRequest):
    """在文件中进行字符串替换。

    查找文件中的 old_str 并替换为 new_str，采用严格度递减的多级容错匹配链
    （精确 → 行尾/缩进 trim → Unicode 归一化 → 块锚定），命中即止。

    失败时返回结构化 400，body 至少含人类可读 `detail` 与机器可读 `reason`：
    - `not_found`: old_str 未找到（含已尝试的容错级别；若能定位到最相似位置，
      附 `closest={line, snippet, similarity}` 真实文件片段供一轮自纠）
    - `not_unique`: old_str 不唯一（含命中处数与行号；可传 `context` 定位提示消歧，
      或扩大上下文 / 用 replace_all）
    - `disproportionate`: 匹配跨度异常被拒
    - `path_error`: 文件不存在 / 不可读 / 路径非法

    参数:
        request: 包含文件路径、原字符串、新字符串、replace_all 及可选 context 的请求体

    返回:
        FileResponse: 包含替换结果和编辑片段
    """
    try:
        tool = get_edit_tool()
        result = await tool.str_replace(
            request.path,
            request.old_str,
            request.new_str,
            request.replace_all,
            request.context,
        )
        return FileResponse(
            success=True,
            output=result.output,
            error=result.error,
        )
    except MatchError as e:
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "detail": e.detail,
                "reason": e.reason,
                **e.info,
            },
        )
    except ToolError as e:
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "detail": f"字符串替换失败: {e.message}",
                "reason": "path_error",
            },
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "detail": f"字符串替换失败: {str(e)}",
                "reason": "internal_error",
            },
        )


@router.post("/insert", response_model=FileResponse, summary="在指定行插入内容")
async def insert_in_file(request: InsertRequest):
    """在文件的指定行位置插入内容。

    在 insert_line 行号处插入新文本，原有内容向下移动。

    参数:
        request: 包含文件路径、行号和插入文本的请求体

    返回:
        FileResponse: 包含插入结果和编辑片段
    """
    try:
        tool = get_edit_tool()
        result = await tool.insert(request.path, request.insert_line, request.insert_text)
        return FileResponse(
            success=True,
            output=result.output,
            error=result.error,
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"行插入失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"行插入失败: {str(e)}")


# ==================== 文件上传/下载 ====================


def _validate_path(path: str) -> Path:
    """校验路径安全性，防止路径遍历攻击。"""
    resolved = Path(path).resolve()
    if ".." in Path(path).parts:
        raise HTTPException(status_code=400, detail="路径中不允许包含 '..'")
    return resolved


class _StorageAwareUploadRoute(APIRoute):
    """Parse before FastAPI masks multipart I/O errors as a generic HTTP 400.

    Keep the endpoint's UploadFile/Form declaration for validation and OpenAPI.
    Starlette caches FormData on Request; its parser tracks every temporary file,
    including unfinished parts absent from FormData when a write fails.
    """

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
            if content_type != "multipart/form-data":
                return await original(request)
            parser = MultiPartParser(request.headers, request.stream())
            try:
                try:
                    request._form = await parser.parse()
                except OSError as exc:
                    code = 507 if exc.errno in (errno.ENOSPC, errno.EDQUOT) else 500
                    # Do not probe/create another tempfile while handling a
                    # disk-full error; gettempdir() can itself fail uncached.
                    temp_root = tempfile.tempdir or os.getenv("TMPDIR") or "/tmp"
                    path = str(Path(os.fsdecode(temp_root)) / "multipart-upload")
                    raise HTTPException(status_code=code, detail=filesystem_error_detail(path, exc)) from exc
                except MultiPartException as exc:
                    raise HTTPException(status_code=400, detail=exc.message) from exc
                except Exception as exc:
                    raise HTTPException(status_code=400, detail="上传表单解析失败，请检查 multipart/form-data 格式") from exc
                return await original(request)
            finally:
                # parse() itself only cleans up MultiPartException. Also close
                # incomplete files on OSError, validation failure or cancellation.
                def close_files():
                    for uploaded in parser._files_to_close_on_error:
                        with suppress(OSError):
                            uploaded.close()

                with anyio.CancelScope(shield=True):
                    await run_in_threadpool(close_files)

        return handler


async def upload_file(
    file: UploadFile,
    dest_path: str = Form(..., description="沙盒内目标绝对路径（目录或完整文件路径）"),
):
    """上传文件到沙盒指定路径。

    通过 multipart/form-data 上传文件，保存到沙盒文件系统。
    如果 dest_path 是目录，则使用上传文件的原始文件名；
    如果 dest_path 是完整文件路径，则直接写入该路径。
    自动创建不存在的中间目录。

    参数:
        file: 上传的文件
        dest_path: 沙盒内目标路径

    返回:
        FileResponse: 包含保存路径和文件大小
    """
    try:
        target = _validate_path(dest_path)

        if target.is_dir() or dest_path.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            filename = Path((file.filename or "uploaded_file").replace("\\", "/")).name
            if filename in ("", ".", ".."):
                raise HTTPException(status_code=400, detail="文件名无效")
            target = target / filename
        else:
            target.parent.mkdir(parents=True, exist_ok=True)

        # multipart 已落 SpooledTemporaryFile；有界拷贝到同目录临时文件后原子替换。
        # 不把大文件重新载入内存，超额/磁盘错误保留目标旧内容并清理半成品。
        size = await run_in_threadpool(_save_upload, file.file, target)
        return FileResponse(
            success=True,
            output=f"文件已保存到 {target}（{size} 字节）",
        )
    except HTTPException:
        raise
    except OSError as exc:
        code = 507 if exc.errno in (errno.ENOSPC, errno.EDQUOT) else 500
        raise HTTPException(status_code=code, detail=filesystem_error_detail(dest_path, exc)) from exc
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"上传文件失败: {str(e)}")


router.add_api_route(
    "/upload", upload_file, methods=["POST"], summary="上传文件到沙盒",
    route_class_override=_StorageAwareUploadRoute,
)


def _save_upload(source, target: Path) -> int:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".upload-", delete=False) as output:
            temporary = Path(output.name)
            size = 0
            while chunk := source.read(_UPLOAD_CHUNK_BYTES):
                size += len(chunk)
                if MAX_UPLOAD_SIZE > 0 and size > MAX_UPLOAD_SIZE:
                    raise HTTPException(status_code=413, detail=f"文件大小超过限制（最大 {MAX_UPLOAD_SIZE} 字节）")
                output.write(chunk)
        mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
        temporary.chmod(mode)
        os.replace(temporary, target)
        return size
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@router.get("/download", summary="从沙盒下载文件")
async def download_file(
    path: str = Query(..., description="沙盒内文件的绝对路径"),
):
    """从沙盒下载指定文件。

    参数:
        path: 沙盒内文件的绝对路径

    返回:
        文件流响应，可直接下载
    """
    try:
        target = _validate_path(path)

        if not target.exists():
            raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
        if not target.is_file():
            raise HTTPException(status_code=400, detail=f"路径不是文件: {path}")

        return FastAPIFileResponse(
            path=str(target),
            filename=target.name,
            media_type="application/octet-stream",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"下载文件失败: {str(e)}")
