"""
工作区任务来源台账（SandboxHub#47，跟进 createrole_com_api#38）

平台按「回合租约 + 归档台账」判定交付文件属于哪个回合；后台任务在回合结束后写出的成果
须能追溯到发起它的回合。本模块按工作区持久化每个终端任务的来源与起止时间：

- 来源：Hub 经 proxy 启动终端任务时，把实例当前的 (session_id, turn_id) 以受信头
  ``X-Sandbox-Job-Origin`` 交给容器；容器把它随 job 落盘，并在 ``/api/terminal/activity``
  里回报任务清单（起止时间取容器内 ``time.time()``——容器与宿主共用内核实时钟）。
- 观测：每次 activity 查询（平台闲置检查、``/jobs``、对账周期）把容器清单并入本台账；
  台账在宿主 ``WORK_AREA_ROOT/job-ledger/<work_area_id>.json``，实例回收重建后不丢。
- 结束：容器回报的结束时间是「最后一个带该 job 标记的进程消失」的上界（精度约 1 秒）；
  实例销毁或停止时仍未结束的任务按销毁时刻结束（上界）。
- 不完整：实例在最后一次成功观测之后又启动过终端任务、却没能再观测就没了，记一段
  ``gap``；容器回报的无来源任务也照记。两者在保留期内都让 ``/jobs`` 返回 ``known=false``。
- 清理：结束超过 ``WORK_AREA_JOB_MAX_LIFETIME``（0 时按 1 天）的任务与 gap 删除。

所有时间在台账内为 epoch 秒（float），对外格式化为带 ``Z`` 的 ISO-8601 UTC。
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

from src.config import settings
from src.manager.paths import require_id

SCHEMA_VERSION = 1
JOB_ORIGIN_HEADER = "X-Sandbox-Job-Origin"
# 经 proxy 启动终端任务的容器路径（相对，无前导斜杠）。长驻进程（api/procs，SandboxHub#52）不在此列：
# 一个引擎进程跨多个产品回合，按启动时的回合打标会把之后各回合的产出都记到首个回合名下；
# 它也不进本台账（容器只回报终端 job），活性另经 activity 的 running_jobs / running_procs 体现。
TERMINAL_START_PATHS = frozenset({"api/terminal/execute", "api/terminal/execute/stream"})
JOB_ID_RE = re.compile(r"^j_[0-9A-HJKMNP-TV-Z]{26}$")
DEFAULT_RETENTION = 86400.0


def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def retention_seconds() -> float:
    return float(settings.WORK_AREA_JOB_MAX_LIFETIME or DEFAULT_RETENTION)


@dataclass(frozen=True)
class ReportedJob:
    job_id: str
    session_id: Optional[str]
    turn_id: Optional[str]
    started_at: float
    ended_at: Optional[float]


@dataclass(frozen=True)
class JobsReport:
    """容器 activity 里的任务清单（已校验）。"""

    jobs: tuple[ReportedJob, ...]
    unattributed_live: tuple[str, ...]


def _number(value) -> bool:
    return type(value) in (int, float) and value >= 0


def _opt_str(value) -> bool:
    return value is None or isinstance(value, str)


def parse_report(data: dict) -> Optional[JobsReport]:
    """容器 activity 响应 → 任务清单；没有 ``jobs`` 字段（旧镜像）返回 None，形状不对抛 ValueError。"""
    if "jobs" not in data:
        return None
    raw_jobs, raw_live = data.get("jobs"), data.get("unattributed_live", [])
    if not isinstance(raw_jobs, list) or not isinstance(raw_live, list):
        raise ValueError("任务清单形状无效")
    jobs = []
    for item in raw_jobs:
        if not isinstance(item, dict):
            raise ValueError("任务条目形状无效")
        job_id, ended = item.get("job_id"), item.get("ended_at")
        if (
            not isinstance(job_id, str)
            or not JOB_ID_RE.match(job_id)
            or not _number(item.get("started_at"))
            or not (ended is None or _number(ended))
            or not _opt_str(item.get("session_id"))
            or not _opt_str(item.get("turn_id"))
        ):
            raise ValueError(f"任务条目字段无效：{job_id!r}")
        jobs.append(
            ReportedJob(
                job_id=job_id,
                session_id=item.get("session_id"),
                turn_id=item.get("turn_id"),
                started_at=float(item["started_at"]),
                ended_at=None if ended is None else float(ended),
            )
        )
    for job_id in raw_live:
        if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
            raise ValueError("无来源任务 ID 无效")
    return JobsReport(jobs=tuple(jobs), unattributed_live=tuple(raw_live))


class JobLedger:
    """按工作区持久化的任务来源台账。全部方法同步、不 await，在事件循环内天然互斥。"""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._cache: dict[str, dict] = {}
        # 进程内：每实例经 proxy 启动终端任务的序号与在途数（判断一次观测能否覆盖全部已启动任务）
        self._start_seq: defaultdict[str, int] = defaultdict(int)
        self._in_flight: defaultdict[str, int] = defaultdict(int)

    # ── 存取 ────────────────────────────────────────────────────────────────

    def _path(self, work_area_id: str) -> Path:
        require_id(work_area_id, "工作区 ID")
        return self._root / f"{work_area_id}.json"

    @staticmethod
    def _empty(work_area_id: str) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "work_area_id": work_area_id,
            "jobs": {},
            "unobserved": {},
            "gaps": [],
        }

    def _load(self, work_area_id: str, now: float) -> dict:
        cached = self._cache.get(work_area_id)
        if cached is not None:
            return cached
        path = self._path(work_area_id)
        try:
            value = json.loads(path.read_text())
            if (
                value.get("schema_version") != SCHEMA_VERSION
                or value.get("work_area_id") != work_area_id
                or not isinstance(value.get("jobs"), dict)
                or not isinstance(value.get("unobserved"), dict)
                or not isinstance(value.get("gaps"), list)
            ):
                raise ValueError("台账形状无效")
        except FileNotFoundError:
            value = self._empty(work_area_id)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            # 读不出的台账不能当作「没有任务」：记一段 gap，保留期内报告 known=false
            logger.warning(f"任务台账损坏，按来源未知处理 | work_area={work_area_id} | err={exc}")
            value = self._empty(work_area_id)
            value["gaps"].append({"start": 0.0, "end": now, "reason": "ledger_unreadable"})
            self._cache[work_area_id] = value
            self._save(work_area_id)
        self._cache[work_area_id] = value
        return value

    def _save(self, work_area_id: str) -> None:
        path = self._path(work_area_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(self._cache[work_area_id], stream, ensure_ascii=False)
        temporary.replace(path)

    def exists(self, work_area_id: str) -> bool:
        return work_area_id in self._cache or self._path(work_area_id).is_file()

    def remove(self, work_area_id: str) -> None:
        self._cache.pop(work_area_id, None)
        self._path(work_area_id).unlink(missing_ok=True)

    # ── 启动打点（proxy）─────────────────────────────────────────────────────

    def start_begin(self, work_area_id: str, sandbox_id: str, now: float) -> None:
        """经 proxy 启动终端任务：在途 +1；实例进入「有未观测启动」状态并落盘。"""
        self._start_seq[sandbox_id] += 1
        self._in_flight[sandbox_id] += 1
        ledger = self._load(work_area_id, now)
        if sandbox_id not in ledger["unobserved"]:
            ledger["unobserved"][sandbox_id] = now
            self._save(work_area_id)

    def start_end(self, sandbox_id: str) -> None:
        self._in_flight[sandbox_id] = max(0, self._in_flight[sandbox_id] - 1)

    def observation_token(self, sandbox_id: str) -> Optional[int]:
        """观测开始前取：没有在途启动时返回当前序号；有在途启动返回 None（本次观测不能清除未观测状态）。"""
        if self._in_flight[sandbox_id]:
            return None
        return self._start_seq[sandbox_id]

    def needs_observation(self, work_area_id: str, sandbox_id: str, now: float) -> bool:
        ledger = self._load(work_area_id, now)
        return sandbox_id in ledger["unobserved"] or any(
            j["sandbox_id"] == sandbox_id and j["ended_at"] is None
            for j in ledger["jobs"].values()
        )

    # ── 观测合并 ────────────────────────────────────────────────────────────

    def merge(
        self,
        work_area_id: str,
        sandbox_id: str,
        report: JobsReport,
        now: float,
        token: Optional[int],
    ) -> None:
        """把一次成功观测并入台账。已结束的任务不再重开；来源以首次记录为准。"""
        ledger = self._load(work_area_id, now)
        jobs = ledger["jobs"]
        reported = set()
        for item in report.jobs:
            reported.add(item.job_id)
            current = jobs.get(item.job_id)
            if current is None:
                jobs[item.job_id] = {
                    "session_id": item.session_id,
                    "turn_id": item.turn_id,
                    "sandbox_id": sandbox_id,
                    "started_at": item.started_at,
                    "ended_at": item.ended_at,
                }
            elif current["ended_at"] is None and item.ended_at is not None:
                current["ended_at"] = item.ended_at
        live = set(report.unattributed_live)
        for job_id in live - reported:
            # 容器里活着却没有来源记录的任务：来源未知，保留期内报告 known=false
            jobs.setdefault(
                job_id,
                {
                    "session_id": None,
                    "turn_id": None,
                    "sandbox_id": sandbox_id,
                    "started_at": now,
                    "ended_at": None,
                },
            )
        for job_id, job in jobs.items():
            if (
                job["sandbox_id"] == sandbox_id
                and job["ended_at"] is None
                and job_id not in reported
                and job_id not in live
            ):
                # 容器已不再回报、也没有存活进程：最迟在本次观测时已结束
                job["ended_at"] = now
        if token is not None and token == self._start_seq[sandbox_id]:
            ledger["unobserved"].pop(sandbox_id, None)
        self._prune(ledger, now)
        self._save(work_area_id)

    def settle_sandbox(self, work_area_id: str, sandbox_id: str, now: float) -> None:
        """实例已销毁或停止：仍未结束的任务按 ``now`` 结束；未观测的启动转为 gap。"""
        ledger = self._load(work_area_id, now)
        if self._settle(ledger, {sandbox_id}, now):
            self._save(work_area_id)
        self._start_seq.pop(sandbox_id, None)
        self._in_flight.pop(sandbox_id, None)

    @staticmethod
    def _settle(ledger: dict, sandboxes: set[str], now: float) -> bool:
        changed = False
        for job in ledger["jobs"].values():
            if job["sandbox_id"] in sandboxes and job["ended_at"] is None:
                job["ended_at"] = now
                changed = True
        for sandbox_id in sandboxes & set(ledger["unobserved"]):
            since = ledger["unobserved"].pop(sandbox_id)
            ledger["gaps"].append({"start": since, "end": now, "reason": "unobserved_starts"})
            changed = True
        return changed

    @staticmethod
    def _prune(ledger: dict, now: float) -> bool:
        cutoff = now - retention_seconds()
        jobs = ledger["jobs"]
        stale = [k for k, j in jobs.items() if j["ended_at"] is not None and j["ended_at"] < cutoff]
        for key in stale:
            del jobs[key]
        gaps = [g for g in ledger["gaps"] if g["end"] >= cutoff]
        changed = bool(stale) or len(gaps) != len(ledger["gaps"])
        ledger["gaps"] = gaps
        return changed

    # ── 读取 ────────────────────────────────────────────────────────────────

    def snapshot(
        self, work_area_id: str, live_sandboxes: set[str], now: float
    ) -> tuple[list[dict], bool]:
        """返回 (有来源的任务清单, 台账是否完整)。不在 ``live_sandboxes`` 里的实例视为已不存在。"""
        ledger = self._load(work_area_id, now)
        dead = {
            j["sandbox_id"] for j in ledger["jobs"].values() if j["ended_at"] is None
        } | set(ledger["unobserved"])
        changed = self._settle(ledger, dead - live_sandboxes, now)
        changed = self._prune(ledger, now) or changed
        if changed:
            self._save(work_area_id)
        complete = not ledger["gaps"]
        out = []
        for job_id, job in ledger["jobs"].items():
            if job["session_id"] is None or job["turn_id"] is None:
                complete = False
                continue
            out.append(
                {
                    "job_id": job_id,
                    "session_id": job["session_id"],
                    "turn_id": job["turn_id"],
                    "started_at": iso_utc(job["started_at"]),
                    "ended_at": None if job["ended_at"] is None else iso_utc(job["ended_at"]),
                }
            )
        out.sort(key=lambda j: (j["started_at"], j["job_id"]))
        return out, complete
