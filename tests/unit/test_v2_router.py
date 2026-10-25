"""v2 路由：鉴权前提、请求校验、错误映射、文件读取头、v1 排空。服务层用 mock，不跑 lifespan。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from src.config import settings
from src.main import app
from src.manager.fs_sidecar import FsOpError, ReadResult
from src.manager.netguard import Capabilities
from src.manager.wa_instances import WorkAreaError
from src.models import ContainerInfo, SandboxRecord, WorkAreaInstance
from src.proxy import forwarder
from src.routers import sandboxes as v1_router
from src.routers import v2 as v2_router

KEY = "hub-secret"
HEADERS = {"X-API-Key": KEY}


def _instance() -> WorkAreaInstance:
    return WorkAreaInstance(
        sandbox_id="sb_1", work_area_id="wa1", user_id="u1", role_id="r1", sandbox_type="code",
        api_token="tok", status="ready",
        container_info=ContainerInfo("cid", "cr-wa", "172.30.0.9", "code"),
    )


@pytest.fixture
def service():
    svc = MagicMock()
    svc.capabilities.return_value = {"ok": True, "items": {}}
    svc.run_selftest = AsyncMock(return_value={"ok": False, "refreshed": True})
    svc.acquire = AsyncMock(return_value=(_instance(), False))
    svc.effective_report.return_value = {"cwd": "/workspace/work", "mounts": []}
    svc.is_deleting.return_value = False
    svc.selftest.result = Capabilities()
    svc.registry.list_all.return_value = []
    v2_router.set_dependencies(svc)
    yield svc
    v2_router.set_dependencies(None)


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_HUB_API_KEY", KEY)


async def test_v2_requires_hub_api_key(client, service, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_HUB_API_KEY", "")
    async with client:
        r = await client.post("/v2/work-areas/wa1/acquire", json={"user_id": "u1", "role_id": "r1"})
        assert r.status_code == 503 and r.json()["detail"]["reason"] == "hub_api_auth_required"
        # 能力报告不受此限（要能看到 hub_api_auth 这一项为什么不过）
        r = await client.get("/v2/capabilities")
        assert r.status_code == 200
    service.acquire.assert_not_awaited()


async def test_v2_rejects_wrong_key(client, service, with_key):
    async with client:
        r = await client.post("/v2/work-areas/wa1/acquire", json={"user_id": "u1", "role_id": "r1"})
        assert r.status_code == 401
        r = await client.get("/v2/capabilities")
        assert r.status_code == 401


async def test_capabilities_refresh(client, service, with_key):
    async with client:
        r = await client.get("/v2/capabilities?refresh=true", headers=HEADERS)
    assert r.json() == {"ok": False, "refreshed": True}


async def test_acquire_maps_request_to_spec(client, service, with_key):
    body = {
        "user_id": "u1", "role_id": "r1", "sandbox_type": "code", "session_id": "s1", "turn_id": "t1",
        "mounts": [{"name": "skills", "bucket": "b", "prefix": "roles/r1/skills/", "mode": "ro"}],
        "inputs": [{"path": "a.txt", "bucket": "b", "key": "k", "sha256": "A" * 64, "size": 1}],
        "context": {"README.md": "x"},
        "policy": {"network": {"default": "deny"}},
        "limits": {"memory_mb": 2048, "cpus": 1.5, "pids": 512},
    }
    async with client:
        r = await client.post("/v2/work-areas/wa1/acquire", json=body, headers=HEADERS)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["sandbox_id"] == "sb_1" and data["reused"] is False and data["effective"]["cwd"] == "/workspace/work"
    spec = service.acquire.await_args.args[0]
    assert spec.work_area_id == "wa1" and spec.network_mode == "deny"
    assert spec.mounts[0].key and spec.inputs[0].sha256 == "a" * 64  # 统一小写
    assert spec.limits == {"memory_mb": 2048, "cpus": 1.5, "pids": 512}


async def test_acquire_without_inputs_means_unchanged(client, service, with_key):
    async with client:
        await client.post("/v2/work-areas/wa1/acquire", json={"user_id": "u1", "role_id": "r1"}, headers=HEADERS)
    spec = service.acquire.await_args.args[0]
    assert spec.inputs is None and spec.context is None


@pytest.mark.parametrize(
    "extra,reason",
    [({"layout_version": 2}, "layout_version_unsupported"), ({"cwd": "/workspace"}, "cwd_unsupported")],
)
async def test_acquire_rejects_unsupported_layout(client, service, with_key, extra, reason):
    async with client:
        r = await client.post(
            "/v2/work-areas/wa1/acquire", json={"user_id": "u1", "role_id": "r1", **extra}, headers=HEADERS
        )
    assert r.status_code == 400 and r.json()["detail"]["reason"] == reason


async def test_acquire_busy_maps_to_409_with_holders(client, service, with_key):
    service.acquire.side_effect = WorkAreaError(409, "instance_busy", "busy", holders=[{"sandbox_id": "sb_x"}])
    async with client:
        r = await client.post("/v2/work-areas/wa1/acquire", json={"user_id": "u1", "role_id": "r1"}, headers=HEADERS)
    assert r.status_code == 409
    assert r.json()["detail"] == {"reason": "instance_busy", "message": "busy", "holders": [{"sandbox_id": "sb_x"}]}


async def test_proxy_injects_instance_token(client, service, with_key, monkeypatch):
    service.get.return_value = _instance()
    captured = {}

    async def fake_forward(ip, path, request, api_token="", extra_headers=None):
        captured.update(ip=ip, path=path, token=api_token, hub_key=request.headers.get("x-api-key"))
        from fastapi.responses import JSONResponse

        return JSONResponse({"ok": True})

    monkeypatch.setattr(v2_router, "forward", fake_forward)
    async with client:
        r = await client.post("/v2/sandboxes/sb_1/proxy/api/terminal/execute", json={}, headers=HEADERS)
    assert r.status_code == 200
    assert captured["ip"] == "172.30.0.9" and captured["token"] == "tok" and captured["path"] == "api/terminal/execute"
    service.registry.touch.assert_called_with("sb_1")


async def test_proxy_round_trips_proc_requests_untagged(client, service, with_key):
    """/api/procs 经真实转发器到「容器」：GET 带查询串、POST 数 MB 的 JSON、DELETE 都原样到达；
    不按终端任务打来源（引擎进程跨多个回合，SandboxHub#52）。"""
    service.get.return_value = _instance()
    container = FastAPI()

    @container.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
    async def echo(path: str, request: Request):
        body = await request.body()
        return {
            "method": request.method,
            "path": path,
            "query": dict(request.query_params),
            "size": len(body),
            "token": request.headers.get("x-sandbox-token"),
            "origin": request.headers.get("x-sandbox-job-origin"),
        }

    forwarder._client_pool["172.30.0.9"] = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=container), base_url="http://172.30.0.9:8000"
    )
    start = {"name": "engine", "argv": ["dsh"], "files": [{"path": "/tmp/f", "content_b64": "A" * (3 << 20)}]}
    try:
        async with client:
            posted = (await client.post("/v2/sandboxes/sb_1/proxy/api/procs", json=start, headers=HEADERS)).json()
            listed = (
                await client.get("/v2/sandboxes/sb_1/proxy/api/procs", params={"name": "engine"}, headers=HEADERS)
            ).json()
            deleted = (await client.delete("/v2/sandboxes/sb_1/proxy/api/procs/p_1", headers=HEADERS)).json()
    finally:
        await forwarder.close_client("172.30.0.9")
    assert posted["method"] == "POST" and posted["path"] == "api/procs" and posted["size"] > 3 << 20
    assert listed["method"] == "GET" and listed["query"] == {"name": "engine"}
    assert deleted["method"] == "DELETE" and deleted["path"] == "api/procs/p_1"
    for reply in (posted, listed, deleted):
        assert reply["token"] == "tok" and reply["origin"] is None
    service.terminal_start.assert_not_called()


async def test_policy_apply_broadcasts_to_v2_instances(client, service, with_key):
    service.apply_network_all = AsyncMock(return_value=([{"sandbox_id": "sb_1", "work_area_id": "wa1"}], []))
    async with client:
        r = await client.post(
            "/v2/sandboxes/policy/apply",
            json={"policy": {"network": {"default": "deny"}}, "role_id": "r1"},
            headers=HEADERS,
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["switched"] == [{"sandbox_id": "sb_1", "work_area_id": "wa1"}] and body["failed"] == []
    assert body["effective_policy"]["network"]["default"] == "deny"
    service.apply_network_all.assert_awaited_with("deny", user_id=None, role_id="r1")


async def test_file_content_headers_and_cleanup(client, service, with_key, tmp_path):
    staged = tmp_path / "read-1"
    staged.write_bytes(b"report")
    service.work_areas.read = AsyncMock(
        return_value=ReadResult(path=staged, size=6, mtime_ns=123, sha256="f" * 64, stable=True)
    )
    async with client:
        r = await client.get("/v2/work-areas/wa1/files/content?path=r.md", headers=HEADERS)
    assert r.status_code == 200 and r.content == b"report"
    assert r.headers["x-file-sha256"] == "f" * 64 and r.headers["x-file-stable"] == "1"
    assert r.headers["x-file-mtime-ns"] == "123" and r.headers["x-file-size"] == "6"
    assert not staged.exists()  # 暂存文件随响应删除
    service.work_areas.read.assert_awaited_with("wa1", "deliverables", "r.md")


@pytest.mark.parametrize("code,status", [(3, 404), (2, 400), (4, 409), (1, 500)])
async def test_file_content_fs_errors(client, service, with_key, code, status):
    service.work_areas.read = AsyncMock(side_effect=FsOpError(code, "x"))
    async with client:
        r = await client.get("/v2/work-areas/wa1/files/content?path=a", headers=HEADERS)
    assert r.status_code == status


async def test_checksum_batch_calls_service(client, service, with_key):
    result = {"schema_version": 1, "items": [{"path": "missing", "found": False}]}
    service.work_areas.checksums = AsyncMock(return_value=result)
    async with client:
        response = await client.post(
            "/v2/work-areas/wa1/files/checksums?root=work",
            headers=HEADERS, json={"paths": ["missing"]},
        )
    assert response.status_code == 200 and response.json() == result
    service.work_areas.checksums.assert_awaited_once_with("wa1", "work", ["missing"])


@pytest.mark.parametrize("paths", [[], ["x"] * 129, ["x" * 1025]])
async def test_checksum_batch_rejects_oversized_requests(client, service, with_key, paths):
    service.work_areas.checksums = AsyncMock()
    async with client:
        response = await client.post(
            "/v2/work-areas/wa1/files/checksums", headers=HEADERS, json={"paths": paths},
        )
    assert response.status_code == 422
    service.work_areas.checksums.assert_not_awaited()


async def test_checksum_batch_keeps_auth_and_path_guards(client, service, with_key):
    service.work_areas.checksums = AsyncMock(side_effect=ValueError("非法路径"))
    async with client:
        response = await client.post("/v2/work-areas/wa1/files/checksums", json={"paths": ["secret"]})
        assert response.status_code == 401
        service.work_areas.checksums.assert_not_awaited()
        response = await client.post(
            "/v2/work-areas/wa1/files/checksums", headers=HEADERS, json={"paths": ["../secret"]},
        )
        assert response.status_code == 400


async def test_file_root_restricted(client, service, with_key):
    async with client:
        r = await client.get("/v2/work-areas/wa1/files?root=inputs", headers=HEADERS)
    assert r.status_code == 422


async def test_import_rejected_while_deleting(client, service, with_key):
    service.is_deleting.return_value = True
    async with client:
        r = await client.post("/v2/work-areas/wa1/import", content=b"", headers=HEADERS)
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "work_area_deleting"


# ── v1 排空 ────────────────────────────────────────────────────────────────


@pytest.fixture
def v1_parts(service, monkeypatch):
    registry = MagicMock()
    # v1 路由的模块级依赖可能被其他测试留下 AsyncMock，这里显式指定
    monkeypatch.setattr(v1_router, "_registry", registry)
    monkeypatch.setattr(v1_router, "_warm_pool", None)
    record = SandboxRecord(
        sandbox_id="v1a", container_info=ContainerInfo("c1", "cr-sb", "172.17.0.5", "code"),
        user_id="u1", role_id="r1", status="ready",
    )
    busy = SandboxRecord(
        sandbox_id="v1b", container_info=ContainerInfo("c2", "cr-sb2", "172.17.0.6", "code"),
        user_id="u2", role_id="r1", status="ready",
    )
    alive = [record, busy]
    registry.list_ready.side_effect = lambda: list(alive)
    pool = MagicMock()
    pool.paused = False
    pool.drain = AsyncMock()
    pool.status.return_value = {}
    pool.ensure_pool = AsyncMock()
    reconciler = MagicMock()
    reconciler._probe_terminal_idle = AsyncMock(side_effect=lambda ip: ip == "172.17.0.5")

    async def destroy(rec):
        alive.remove(rec)

    reconciler.destroy_sandbox = AsyncMock(side_effect=destroy)
    v2_router.set_dependencies(service, registry, pool, reconciler)
    yield registry, pool, reconciler, alive
    v1_router.set_draining(False)


async def test_drain_releases_idle_and_reports_busy(client, with_key, v1_parts):
    registry, pool, reconciler, alive = v1_parts
    async with client:
        r = await client.post("/v2/admin/drain", json={"wait_seconds": 0}, headers=HEADERS)
        data = r.json()
        assert r.status_code == 200
        assert data["draining"] is True and data["released"] == ["v1a"] and data["timed_out"] is True
        assert [b["sandbox_id"] for b in data["still_busy"]] == ["v1b"]
        assert pool.paused is True
        pool.drain.assert_awaited()
        # 排空期间 v1 acquire 拒绝
        r = await client.post("/v1/sandboxes/acquire", json={"user_id": "u9", "role_id": "r9"}, headers=HEADERS)
        assert r.status_code == 503
        # force：剩余的一并销毁
        r = await client.post("/v2/admin/drain", json={"wait_seconds": 0, "force": True}, headers=HEADERS)
        assert r.json()["released"] == ["v1b"] and r.json()["still_busy"] == []
        assert alive == []
        # 解除（回滚到 v1）
        r = await client.delete("/v2/admin/drain", headers=HEADERS)
        assert r.json()["draining"] is False and pool.paused is False


async def test_health_reports_work_area_summary(client, service, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_HUB_API_KEY", "")
    monkeypatch.setattr(v1_router, "_registry", None)
    monkeypatch.setattr(v1_router, "_warm_pool", None)
    async with client:
        r = await client.get("/v1/health")
    body = r.json()
    assert body["work_area"]["ok"] is False  # 自检未跑
    assert body["draining"] is False


async def test_files_cutover_requires_drain_and_paused_pool(client, with_key, v1_parts, service):
    registry, pool, reconciler, alive = v1_parts
    service.set_files_cutover = AsyncMock()
    async with client:
        r = await client.post('/v2/admin/files-cutover', headers=HEADERS)
        assert r.status_code == 409
        alive.clear()
        v1_router.set_draining(True)
        r = await client.post('/v2/admin/files-cutover', headers=HEADERS)
        assert r.status_code == 409
        pool.paused = True
        r = await client.post('/v2/admin/files-cutover', headers=HEADERS)
        assert r.status_code == 200 and r.json()['minio_forward'] is False
    service.set_files_cutover.assert_awaited_once_with(True)


async def test_cutover_state_survives_dependency_rebuild_and_rollback(client, with_key, v1_parts, service, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'WORK_AREA_ROOT', str(tmp_path))
    (tmp_path/'.files-cutover.json').write_text('{"schema_version":1}')
    registry, pool, reconciler, alive = v1_parts
    service.set_files_cutover = AsyncMock(side_effect=lambda value: (tmp_path/'.files-cutover.json').unlink())
    v1_router.set_draining(False)
    pool.paused = False
    v2_router.set_dependencies(service, registry, pool, reconciler)
    assert v1_router.is_draining() and pool.paused
    async with client:
        r = await client.delete('/v2/admin/drain', headers=HEADERS)
        assert r.status_code == 200 and not r.json()['files_cutover']
    service.set_files_cutover.assert_awaited_once_with(False)


def test_v2_gateway_and_cutover_filter_manual_minio_forwards(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'WORK_AREA_ROOT', str(tmp_path))
    monkeypatch.setattr(settings, 'MINIO_ENDPOINT', 'minio.internal:9000')
    monkeypatch.setattr(settings, 'GATEWAY_MINIO_FORWARD', True)
    monkeypatch.setattr(settings, 'SANDBOX_GATEWAY_FORWARDS', '9000=minio.internal:9000,10000=minio.internal:9000,8080=api.internal:8011')
    assert settings.gateway_forwards(include_minio=False) == [(8080, 'api.internal:8011')]
    assert (9000, 'minio.internal:9000') in settings.gateway_forwards()
    (tmp_path/'.files-cutover.json').write_text('{"schema_version":1}')
    assert settings.gateway_forwards() == [(8080, 'api.internal:8011')]
