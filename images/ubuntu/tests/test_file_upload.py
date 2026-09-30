import errno
import io
import asyncio
import tempfile

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.routers import file as module
from starlette import formparsers


class BoundedSource(io.BytesIO):
    def read(self, size=-1):
        assert 0 < size <= 1024 * 1024
        return super().read(size)


def test_unlimited_upload_uses_bounded_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "MAX_UPLOAD_SIZE", 0)
    data = b"a" * (3 * 1024 * 1024 + 17)
    target = tmp_path / "data.bin"
    assert module._save_upload(BoundedSource(data), target) == len(data)
    assert target.read_bytes() == data
    assert not list(tmp_path.glob(".upload-*"))


def test_failed_upload_preserves_existing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "MAX_UPLOAD_SIZE", 2)
    target = tmp_path / "data.bin"
    target.write_bytes(b"old")
    with pytest.raises(HTTPException) as error:
        module._save_upload(BoundedSource(b"new larger"), target)
    assert error.value.status_code == 413
    assert target.read_bytes() == b"old"
    assert not list(tmp_path.glob(".upload-*"))


def test_upload_disk_full_is_507_and_names_storage_path(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(module, "_save_upload", fail)
    app = FastAPI()
    app.include_router(module.router)
    with TestClient(app) as client:
        response = client.post("/api/file/upload", data={"dest_path": str(tmp_path / "x")}, files={"file": ("x", b"content")})
    assert response.status_code == 507
    assert "Docker" in response.json()["detail"]
    assert str(tmp_path) in response.json()["detail"]


@pytest.mark.parametrize("error_number", [errno.ENOSPC, errno.EDQUOT])
def test_multipart_spill_storage_error_is_507_and_closes_spool(tmp_path, monkeypatch, error_number):
    spools = []
    real_spool = tempfile.SpooledTemporaryFile

    def spool(*args, **kwargs):
        file = real_spool(*args, **kwargs)
        spools.append(file)
        return file

    def fail_rollover(*args, **kwargs):
        raise OSError(error_number, "storage full")

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", spool)
    monkeypatch.setattr(tempfile, "TemporaryFile", fail_rollover)
    app = FastAPI()
    app.include_router(module.router)
    target = tmp_path / "old"
    target.write_bytes(b"old content")
    with TestClient(app) as client:
        response = client.post("/api/file/upload", data={"dest_path": str(target)}, files={"file": ("x", b"x" * (1024 * 1024 + 1))})
    assert response.status_code == 507
    assert "multipart-upload" in response.json()["detail"]
    assert f"errno={error_number}" in response.json()["detail"]
    assert spools and all(file.closed for file in spools)
    assert target.read_bytes() == b"old content"
    assert not list(tmp_path.glob(".upload-*"))


def test_multipart_creation_storage_error_is_not_masked(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", fail)
    app = FastAPI()
    app.include_router(module.router)
    with TestClient(app) as client:
        response = client.post("/api/file/upload", data={"dest_path": "/tmp/unused"}, files={"file": ("x", b"content")})
    assert response.status_code == 507 and "errno=28" in response.json()["detail"]


@pytest.mark.asyncio
async def test_cancelled_multipart_parse_closes_incomplete_files(monkeypatch):
    spools = []
    real_spool = tempfile.SpooledTemporaryFile

    def spool(*args, **kwargs):
        file = real_spool(*args, **kwargs)
        spools.append(file)
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", spool)
    received = False

    async def receive():
        nonlocal received
        if received:
            raise asyncio.CancelledError()
        received = True
        return {"type": "http.request", "more_body": True, "body": b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="x"\r\n\r\npartial'}

    request = Request({"type": "http", "method": "POST", "path": "/upload", "headers": [(b"content-type", b"multipart/form-data; boundary=boundary")]}, receive)
    route = module._StorageAwareUploadRoute("/upload", module.upload_file, methods=["POST"])
    with pytest.raises(asyncio.CancelledError):
        await route.get_route_handler()(request)
    assert spools and all(file.closed for file in spools)


def test_upload_keeps_multipart_schema_and_validation(tmp_path):
    app = FastAPI()
    app.include_router(module.router)
    schema = app.openapi()
    request_schema = schema["paths"]["/api/file/upload"]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]
    body = schema["components"]["schemas"][request_schema["$ref"].rsplit("/", 1)[1]]
    assert set(body["required"]) == {"file", "dest_path"}
    with TestClient(app) as client:
        missing = client.post("/api/file/upload", files={"file": ("x", b"content")})
        assert missing.status_code == 422
        malformed = client.post("/api/file/upload", content=b"broken body", headers={"content-type": "multipart/form-data; boundary=boundary"})
        assert malformed.status_code == 400
        response = client.post("/api/file/upload", data={"dest_path": str(tmp_path / "result")}, files={"file": ("x", b"content")})
        assert response.status_code == 200
    assert (tmp_path / "result").read_bytes() == b"content"
