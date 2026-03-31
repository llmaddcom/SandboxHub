"""
文件操作路由模块 - 提供文件查看、创建和编辑的 API 接口。

接口列表：
- POST /api/file/view: 查看文件/目录内容
- POST /api/file/create: 创建新文件
- POST /api/file/replace: 字符串替换编辑
- POST /api/file/insert: 在指定行插入内容
"""

import os
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse as FastAPIFileResponse
from pydantic import BaseModel, Field

from ..tools import EditTool, ToolError

MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100 MB

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


class CreateRequest(BaseModel):
    """创建文件请求模型。"""
    path: str = Field(..., description="新文件的绝对路径", examples=["/home/user/new_file.py"])
    file_text: str = Field(..., description="文件内容")


class ReplaceRequest(BaseModel):
    """字符串替换请求模型。"""
    path: str = Field(..., description="文件的绝对路径", examples=["/home/user/test.py"])
    old_str: str = Field(..., description="要替换的原始字符串")
    new_str: str | None = Field(default=None, description="替换后的新字符串（为空则删除原字符串）")


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


# ==================== API 接口 ====================

@router.post("/view", response_model=FileResponse, summary="查看文件/目录内容")
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
        result = await tool.view(request.path, request.view_range)
        return FileResponse(
            success=True,
            output=result.output,
            error=result.error,
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

    查找文件中的 old_str 并替换为 new_str。
    old_str 在文件中必须是唯一的，否则替换将失败。

    参数:
        request: 包含文件路径、原字符串和新字符串的请求体

    返回:
        FileResponse: 包含替换结果和编辑片段
    """
    try:
        tool = get_edit_tool()
        result = await tool.str_replace(request.path, request.old_str, request.new_str)
        return FileResponse(
            success=True,
            output=result.output,
            error=result.error,
        )
    except ToolError as e:
        raise HTTPException(status_code=400, detail=f"字符串替换失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"字符串替换失败: {str(e)}")


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


@router.post("/upload", summary="上传文件到沙盒")
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
            filename = file.filename or "uploaded_file"
            target = target / filename
        else:
            target.parent.mkdir(parents=True, exist_ok=True)

        content = await file.read()
        if len(content) > MAX_UPLOAD_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"文件大小超过限制（最大 {MAX_UPLOAD_SIZE // 1024 // 1024} MB）",
            )

        target.write_bytes(content)
        return FileResponse(
            success=True,
            output=f"文件已保存到 {target}（{len(content)} 字节）",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"上传文件失败: {str(e)}")


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
