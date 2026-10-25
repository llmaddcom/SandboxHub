"""任务来源台账（SandboxHub#47）：打标、持久化跨实例重建、/jobs 的 known 语义、清理、files 的 observed_at。

台账用真实实现落在 tmp；容器 activity 用假响应；服务编排复用 test_wa_service 的假 Docker / cr-fs。
"""
from __future__ import annotations

import json
import re
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from src.config import settings
from src.main import app
from src.manager.wa_instances import WorkAreaError, WorkAreaService
from src.manager.wa_jobs import JOB_ORIGIN_HEADER, JobLedger, JobsReport, ReportedJob, iso_utc, parse_report
from src.models import ContainerInfo, WorkAreaInstance
from src.routers import v2 as v2_router
from tests.unit.test_wa_service import _spec, env  # noqa: F401  (pytest fixture)

J1 = "j_01ARZ3NDEKTSV4RRFFQ69G5FA1"
J2 = "j_01ARZ3NDEKTSV4RRFFQ69G5FA2"
J3 = "j_01ARZ3NDEKTSV4RRFFQ69G5FA3"
ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


def _report(*jobs: tuple, live: tuple = ()) -> JobsReport:
    return JobsReport(jobs=tuple(ReportedJob(*j) for j in jobs), unattributed_live=live)


def _activity(*jobs: dict, live: list | None = None, marked: int = 0) -> dict:
    return {
        "running_jobs": 0,
        "live_panes": 0,
        "marked_processes": marked,
        "jobs": list(jobs),
        "unattributed_live": live or [],
    }


def _job(job_id, session="s1", turn="t1", started=100.0, ended=None) -> dict:
    return {"job_id": job_id, "session_id": session, "turn_id": turn, "started_at": started, "ended_at": ended}


# ── 台账 ────────────────────────────────────────────────────────────────────


def test_parse_report_shapes():
    assert parse_report({"running_jobs": 0}) is None  # 旧镜像：不回报清单
    report = parse_report(_activity(_job(J1, ended=101), live=[J2]))
    assert report.jobs[0].ended_at == 101.0 and report.unattributed_live == (J2,)
    for bad in (
        {"jobs": "x"},
        {"jobs": [_job("not-a-job")]},
        {"jobs": [dict(_job(J1), started_at="yesterday")]},
        {"jobs": [], "unattributed_live": ["../x"]},
    ):
        with pytest.raises(ValueError):
            parse_report(bad)


def test_ledger_persists_and_reloads(tmp_path):
    ledger = JobLedger(tmp_path)
    ledger.merge("wa1", "sb_a", _report((J1, "s1", "t1", 100.0, None)), now=110.0, token=0)
    reloaded = JobLedger(tmp_path)  # Hub 重启 / 实例重建后从宿主文件恢复
    jobs, complete = reloaded.snapshot("wa1", {"sb_a"}, now=120.0)
    assert complete and jobs == [
        {"job_id": J1, "session_id": "s1", "turn_id": "t1", "started_at": iso_utc(100.0), "ended_at": None}
    ]
    assert (tmp_path / "wa1.json").stat().st_mode & 0o777 == 0o600


def test_ended_job_is_never_reopened_and_first_origin_wins(tmp_path):
    ledger = JobLedger(tmp_path)
    ledger.merge("wa1", "sb_a", _report((J1, "s1", "t1", 100.0, 105.0)), now=110.0, token=0)
    ledger.merge("wa1", "sb_a", _report((J1, "s9", "t9", 100.0, None)), now=120.0, token=0)
    [job], _ = ledger.snapshot("wa1", {"sb_a"}, now=130.0)
    assert job["ended_at"] == iso_utc(105.0) and job["turn_id"] == "t1"


def test_open_job_missing_from_report_is_closed_at_observation(tmp_path):
    ledger = JobLedger(tmp_path)
    ledger.merge("wa1", "sb_a", _report((J1, "s1", "t1", 100.0, None), (J2, "s1", "t1", 100.0, None)), now=110.0, token=0)
    # J1 不再回报但仍有存活进程（容器侧记录丢了）：保持未结束；J2 既不回报也不存活：按观测时刻结束
    ledger.merge("wa1", "sb_a", _report(live=(J1,)), now=120.0, token=0)
    jobs, complete = ledger.snapshot("wa1", {"sb_a"}, now=130.0)
    assert {j["job_id"]: j["ended_at"] for j in jobs} == {J1: None, J2: iso_utc(120.0)}
    assert complete


def test_unattributed_live_job_makes_ledger_unknown_until_retention(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "WORK_AREA_JOB_MAX_LIFETIME", 100)
    ledger = JobLedger(tmp_path)
    ledger.merge("wa1", "sb_a", _report((J1, None, None, 10.0, 20.0), live=(J2,)), now=30.0, token=0)
    jobs, complete = ledger.snapshot("wa1", {"sb_a"}, now=40.0)
    assert jobs == [] and not complete  # 无来源任务不输出，但清单不完整
    ledger.settle_sandbox("wa1", "sb_a", now=50.0)  # J2 随实例销毁结束于 50
    _, complete = ledger.snapshot("wa1", set(), now=149.0)
    assert not complete
    _, complete = ledger.snapshot("wa1", set(), now=151.0)  # 结束超过保留期后清理
    assert complete


def test_unobserved_starts_become_gap_when_instance_dies(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "WORK_AREA_JOB_MAX_LIFETIME", 100)
    ledger = JobLedger(tmp_path)
    ledger.start_begin("wa1", "sb_a", now=10.0)
    ledger.start_end("sb_a")
    ledger.settle_sandbox("wa1", "sb_a", now=20.0)  # 销毁前没能观测
    _, complete = ledger.snapshot("wa1", set(), now=30.0)
    assert not complete
    data = json.loads((tmp_path / "wa1.json").read_text())
    assert data["gaps"] == [{"start": 10.0, "end": 20.0, "reason": "unobserved_starts"}]
    _, complete = ledger.snapshot("wa1", set(), now=121.0)
    assert complete and json.loads((tmp_path / "wa1.json").read_text())["gaps"] == []


def test_observation_clears_unobserved_only_when_it_covers_all_starts(tmp_path):
    ledger = JobLedger(tmp_path)
    ledger.start_begin("wa1", "sb_a", now=10.0)
    in_flight = ledger.observation_token("sb_a")
    assert in_flight is None  # 启动请求在途：本次观测可能看不到它
    ledger.merge("wa1", "sb_a", _report(), now=11.0, token=in_flight)
    assert ledger.needs_observation("wa1", "sb_a", now=12.0)
    ledger.start_end("sb_a")
    token = ledger.observation_token("sb_a")
    ledger.start_begin("wa1", "sb_a", now=13.0)  # 观测期间又来一个启动
    ledger.start_end("sb_a")
    ledger.merge("wa1", "sb_a", _report(), now=14.0, token=token)
    assert ledger.needs_observation("wa1", "sb_a", now=15.0)
    ledger.merge("wa1", "sb_a", _report(), now=16.0, token=ledger.observation_token("sb_a"))
    assert not ledger.needs_observation("wa1", "sb_a", now=17.0)


def test_snapshot_settles_jobs_of_vanished_instances(tmp_path):
    ledger = JobLedger(tmp_path)
    ledger.merge("wa1", "sb_old", _report((J1, "s1", "t1", 100.0, None)), now=110.0, token=0)
    jobs, complete = ledger.snapshot("wa1", {"sb_new"}, now=200.0)
    assert jobs[0]["ended_at"] == iso_utc(200.0) and complete


def test_retention_cleans_ended_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "WORK_AREA_JOB_MAX_LIFETIME", 100)
    ledger = JobLedger(tmp_path)
    ledger.merge(
        "wa1", "sb_a",
        _report((J1, "s1", "t1", 10.0, 20.0), (J2, "s1", "t2", 10.0, 90.0), (J3, "s2", "t3", 10.0, None)),
        now=95.0, token=0,
    )
    jobs, _ = ledger.snapshot("wa1", {"sb_a"}, now=150.0)
    assert [j["job_id"] for j in jobs] == [J2, J3]  # J1 结束超过 100 秒；未结束的从不清理
    assert set(json.loads((tmp_path / "wa1.json").read_text())["jobs"]) == {J2, J3}


def test_unreadable_ledger_is_unknown_not_empty(tmp_path):
    (tmp_path / "wa1.json").write_text("{")
    _, complete = JobLedger(tmp_path).snapshot("wa1", set(), now=time.time())
    assert not complete


# ── 服务编排 ────────────────────────────────────────────────────────────────


@pytest.fixture
def svc(env):  # noqa: F811
    service, manager, sidecar, minio = env
    del service.activity  # 用真实 activity（经 _probe_terminal 取容器响应）
    service._probe_terminal = AsyncMock(return_value=_activity())
    return service, manager


async def test_job_report_lists_tagged_jobs(svc):
    service, _ = svc
    instance, _ = await service.acquire(_spec(session_id="s1", turn_id="t1"))
    service._probe_terminal.return_value = _activity(_job(J1, started=100.0), marked=1)
    report = await service.job_report("wa1")
    assert report["known"] is True and ISO_Z.match(report["now"])
    assert report["jobs"] == [
        {"job_id": J1, "session_id": "s1", "turn_id": "t1", "started_at": iso_utc(100.0), "ended_at": None}
    ]
    # activity 本身也照常回报计数（闲置判定不受影响）
    activity = await service.activity(instance)
    assert activity["known"] and activity["jobs_known"] and activity["marked_processes"] == 1


async def test_activity_keeps_instances_with_running_procs_busy(svc):
    service, _ = svc
    instance, _ = await service.acquire(_spec(session_id="s1", turn_id="t1"))
    # 容器把运行中的长驻进程（引擎）计入 running_jobs，并单列 running_procs；它们不进任务台账
    service._probe_terminal.return_value = dict(_activity(), running_jobs=1, running_procs=1)
    activity = await service.activity(instance)
    assert activity["idle"] is False and activity["running_jobs"] == 1 and activity["running_procs"] == 1
    report = await service.job_report("wa1")
    assert report["known"] is True and report["jobs"] == []
    service._probe_terminal.return_value = _activity()   # 旧镜像不回报 running_procs
    assert (await service.activity(instance))["running_procs"] is None


@pytest.mark.parametrize(
    "probe",
    [
        AsyncMock(side_effect=OSError("unreachable")),
        AsyncMock(return_value={"running_jobs": 0, "live_panes": 0, "marked_processes": 0}),  # 旧镜像
        AsyncMock(return_value=dict(_activity(), jobs=[{"job_id": "bad"}])),
        AsyncMock(return_value=_activity(live=[J2])),  # 存活却无来源
    ],
    ids=["unreachable", "old-image", "invalid", "unattributed"],
)
async def test_job_report_unknown_when_listing_cannot_be_trusted(svc, probe):
    service, _ = svc
    await service.acquire(_spec())
    service._probe_terminal = probe
    report = await service.job_report("wa1")
    assert report["known"] is False


async def test_job_records_survive_instance_rebuild(svc):
    service, _ = svc
    one, _ = await service.acquire(_spec(session_id="s1", turn_id="tA"))
    service._probe_terminal.return_value = _activity(_job(J1, turn="tA", started=100.0))
    await service.activity(one)
    await service.release(one.sandbox_id)  # 销毁前最后一次观测仍见 J1 未结束 → 按销毁时刻结束
    two, reused = await service.acquire(_spec(session_id="s2", turn_id="tB"))
    assert not reused and two.sandbox_id != one.sandbox_id
    service._probe_terminal.return_value = _activity()  # 新实例里没有旧任务
    report = await service.job_report("wa1")
    assert report["known"] is True
    [job] = report["jobs"]
    assert job["turn_id"] == "tA" and job["ended_at"] is not None and job["ended_at"] <= report["now"]


async def test_discard_without_final_observation_after_starts_is_unknown(svc):
    service, manager = svc
    instance, _ = await service.acquire(_spec(session_id="s1", turn_id="t1"))
    async with service.terminal_start(instance):
        pass
    service._probe_terminal.side_effect = OSError("dead")
    await service.release(instance.sandbox_id)
    service._probe_terminal.side_effect = None
    assert (await service.job_report("wa1"))["known"] is False


async def test_discard_after_successful_observation_is_known(svc):
    service, _ = svc
    instance, _ = await service.acquire(_spec(session_id="s1", turn_id="t1"))
    async with service.terminal_start(instance):
        pass
    started, ended = time.time() - 10, time.time() - 5
    service._probe_terminal.return_value = _activity(_job(J1, started=started, ended=ended))
    await service.release(instance.sandbox_id)
    report = await service.job_report("wa1")
    assert report["known"] is True and report["jobs"][0]["ended_at"] == iso_utc(ended)


async def test_quiesce_settles_open_jobs(svc):
    service, manager = svc
    instance, _ = await service.acquire(_spec(session_id="s1", turn_id="t1"))
    service._probe_terminal.return_value = _activity(_job(J1, started=100.0))
    before = time.time()
    await service.quiesce(instance.sandbox_id)
    manager.stop_wa_container.assert_awaited_once()
    report = await service.job_report("wa1")  # 停止的实例不再探测
    assert report["known"] is True and report["jobs"][0]["ended_at"] >= iso_utc(before)


async def test_job_report_errors(svc):
    service, _ = svc
    with pytest.raises(WorkAreaError) as missing:
        await service.job_report("wa-none")
    assert missing.value.status == 404 and missing.value.reason == "work_area_not_found"
    with pytest.raises(WorkAreaError) as invalid:
        await service.job_report("../x")
    assert invalid.value.status == 400


async def test_existing_work_area_without_instance_reports_empty_known(svc):
    service, _ = svc
    await service.work_areas.ensure("wa1")
    assert (await service.job_report("wa1"))["known"] is True


async def test_delete_work_area_removes_ledger(svc):
    service, _ = svc
    instance, _ = await service.acquire(_spec())
    service._probe_terminal.return_value = _activity(_job(J1, ended=101.0))
    await service.activity(instance)
    assert service.jobs.exists("wa1")
    await service.delete_work_area("wa1", force=True)
    assert not service.jobs.exists("wa1")


async def test_reconcile_observes_instances_with_unobserved_starts(svc):
    service, manager = svc
    service.run_selftest = AsyncMock()
    await service.startup()
    instance, _ = await service.acquire(_spec())
    from tests.unit.test_wa_service import _managed

    live = _managed(instance.sandbox_id, "wa1", mounts=())
    live.container_id = instance.container_info.container_id
    manager.list_managed.return_value = [live]
    await service.reconcile_once()
    service._probe_terminal.assert_not_awaited()  # 没有启动过任务：不额外探测
    async with service.terminal_start(instance):
        pass
    await service.reconcile_once()
    service._probe_terminal.assert_awaited_once()
    assert not service.jobs.needs_observation("wa1", instance.sandbox_id, time.time())


def test_origin_header_carries_current_session_and_turn(svc):
    service, _ = svc
    instance = WorkAreaInstance(
        sandbox_id="sb_1", work_area_id="wa1", user_id="u1", role_id="r1", sandbox_type="code",
        api_token="tok", session_id="s-会话", turn_id="t1",
    )
    header = service.origin_header(instance)[JOB_ORIGIN_HEADER]
    header.encode("latin-1")  # 必须是合法 HTTP 头值
    assert json.loads(header) == {"session_id": "s-会话", "turn_id": "t1"}


# ── 路由 ────────────────────────────────────────────────────────────────────

KEY = "hub-secret"
HEADERS = {"X-API-Key": KEY}


@pytest.fixture
def router_service(monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_HUB_API_KEY", KEY)
    service = MagicMock()
    v2_router.set_dependencies(service)
    yield service
    v2_router.set_dependencies(None)


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_jobs_endpoint_and_errors(client, router_service):
    router_service.job_report = AsyncMock(return_value={"known": True, "now": "x", "jobs": []})
    async with client:
        r = await client.get("/v2/work-areas/wa1/jobs", headers=HEADERS)
        assert r.status_code == 200 and r.json() == {"known": True, "now": "x", "jobs": []}
        router_service.job_report.side_effect = WorkAreaError(404, "work_area_not_found", "missing")
        r = await client.get("/v2/work-areas/wa1/jobs", headers=HEADERS)
        assert r.status_code == 404 and r.json()["detail"]["reason"] == "work_area_not_found"
        r = await client.get("/v2/work-areas/wa1/jobs")
        assert r.status_code == 401


async def test_files_listing_reports_observed_at_taken_before_listing(client, router_service):
    seen = {}

    async def list_files(work_area_id, root, max_files):
        seen["during"] = time.time()
        return {"files": [], "truncated": False}

    router_service.work_areas.list_files = list_files
    before = time.time()
    async with client:
        r = await client.get("/v2/work-areas/wa1/files?root=deliverables", headers=HEADERS)
    body = r.json()
    assert r.status_code == 200 and body["files"] == [] and ISO_Z.match(body["observed_at"])
    assert iso_utc(before) <= body["observed_at"] <= iso_utc(seen["during"])


def _proxy_instance() -> WorkAreaInstance:
    return WorkAreaInstance(
        sandbox_id="sb_1", work_area_id="wa1", user_id="u1", role_id="r1", sandbox_type="code",
        api_token="tok", status="ready", session_id="s1", turn_id="t1",
        container_info=ContainerInfo("cid", "cr-wa", "172.30.0.9", "code"),
    )


@pytest.mark.parametrize(
    "path,method,tagged",
    [
        ("api/terminal/execute", "POST", True),
        ("api/terminal/execute/stream", "POST", True),
        ("api/terminal/wait", "POST", False),
        ("api/file/view", "POST", False),
    ],
)
async def test_proxy_tags_terminal_starts_only(client, router_service, monkeypatch, path, method, tagged, tmp_path):
    instance = _proxy_instance()
    real = WorkAreaService.__new__(WorkAreaService)
    real.jobs = JobLedger(tmp_path)
    router_service.get.return_value = instance
    router_service.origin_header = lambda inst: WorkAreaService.origin_header(real, inst)
    router_service.terminal_start = lambda inst: WorkAreaService.terminal_start(real, inst)
    captured = {}

    async def fake_forward(ip, p, request, api_token="", extra_headers=None):
        captured.update(extra=extra_headers, in_flight=real.jobs.observation_token("sb_1") is None)
        from fastapi.responses import JSONResponse

        return JSONResponse({"ok": True})

    monkeypatch.setattr(v2_router, "forward", fake_forward)
    async with client:
        r = await client.request(
            method, f"/v2/sandboxes/sb_1/proxy/{path}", json={},
            headers={**HEADERS, JOB_ORIGIN_HEADER: '{"session_id":"forged","turn_id":"x"}'},
        )
    assert r.status_code == 200
    if tagged:
        assert json.loads(captured["extra"][JOB_ORIGIN_HEADER]) == {"session_id": "s1", "turn_id": "t1"}
        assert captured["in_flight"] and real.jobs.needs_observation("wa1", "sb_1", time.time())
        assert real.jobs.observation_token("sb_1") is not None  # 请求结束后不再在途
    else:
        assert captured["extra"] is None and not real.jobs.exists("wa1")


async def test_forwarder_drops_caller_origin_header_and_injects_hub_one():
    from src.proxy import forwarder

    sent = {}

    class FakeClient:
        async def request(self, method, url, content, headers, params):
            sent.update(headers)
            import httpx

            return httpx.Response(200, json={})

    request = MagicMock()
    request.headers = {"x-sandbox-job-origin": "forged", "content-type": "application/json"}
    request.method = "POST"
    request.query_params = {}
    request.stream = MagicMock(return_value=None)
    original = forwarder._get_client
    forwarder._get_client = lambda ip: FakeClient()
    try:
        await forwarder.forward("1.2.3.4", "api/terminal/execute", request, api_token="tok")
        assert "x-sandbox-job-origin" not in {k.lower() for k in sent}
        sent.clear()
        await forwarder.forward(
            "1.2.3.4", "api/terminal/execute", request, api_token="tok",
            extra_headers={JOB_ORIGIN_HEADER: "hub"},
        )
        assert sent[JOB_ORIGIN_HEADER] == "hub"
    finally:
        forwarder._get_client = original
