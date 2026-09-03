"""
终端工具模块 - 持久会话 + job 化命令执行（issue #30，承接 createrole#367）。

模型：
- 每条命令是一个 job：独立进程组（setsid）运行，stdout/stderr 合并直写
  ``/tmp/cr-jobs/<job_id>.log``（模型可 tail/grep），Python 侧不缓冲输出。
- 「持久会话」= 跨 job 传递的 cwd + 导出环境：job 结束时（EXIT trap）把 ``pwd``
  与 ``env -0`` 落盘，下一个 job 以此为起点。``cd``/``export``/``source venv``
  因而跨调用保留；shell 函数/别名/未导出变量不跨越（与 tmux 相比是可接受的差异）。
- 同一时刻只跑一个 job（会话语义）；running 时再提交 → SessionBusy。
- ``timeout``：无默认、无上限。到期先 SIGINT，宽限后 SIGKILL 整个进程组。
- ``wait``：调用方单次最多等待秒数（默认 30，上限 120），到点先返回 running。
- 兼容旧形态：``BashTool.execute(command, timeout)`` 阻塞至结束（默认 30s、最大
  300s 超时），返回 ToolResult。
"""

import asyncio
import os
import secrets
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

from .base import CLIResult, ToolError, ToolResult

# ---- job 契约参数 ----
DEFAULT_WAIT = 30.0
MAX_WAIT = 120.0
KILL_GRACE = 5.0          # timeout 到期：SIGINT → 等这么久 → SIGKILL
JOB_DIR = Path(os.getenv("CR_JOB_DIR", "/tmp/cr-jobs"))
MAX_JOB_RECORDS = 200     # 内存保留的 job 记录数（日志文件不删）

# ---- 旧契约参数（BashTool.execute / execute_stream）----
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 300.0

# ---- 输出截断（响应体口径；日志文件全量）----
HEAD_BYTES = 25 * 1024   # 25 KB
TAIL_BYTES = 25 * 1024   # 25 KB

STATUS_RUNNING = "running"
STATUS_EXITED = "exited"
STATUS_KILLED = "killed"

_SIGNALS = {"INT": signal.SIGINT, "KILL": signal.SIGKILL, "TERM": signal.SIGTERM}

# 不跨 job 传递的环境变量（由 shell 自行维护）
_ENV_SKIP = {"_", "PWD", "OLDPWD", "SHLVL"}

# job 包装脚本：$1 = 状态文件基名；EXIT trap 保证 exit / 报错 / SIGINT 后仍能保存状态
_WRAPPER = (
    '__cr_state="$1"; set --; '
    '__cr_save() { pwd -P > "$__cr_state.cwd" 2>/dev/null; env -0 > "$__cr_state.env" 2>/dev/null; }; '
    "trap __cr_save EXIT; "
    '. "$__cr_state.sh"'
)


def _head_tail_truncate(text: str) -> str:
    """Keep first HEAD_BYTES + last TAIL_BYTES; replace middle with notice."""
    b = text.encode()
    if len(b) <= HEAD_BYTES + TAIL_BYTES:
        return text
    omitted = len(b) - HEAD_BYTES - TAIL_BYTES
    return (
        b[:HEAD_BYTES].decode(errors="replace")
        + f"\n[...{omitted} bytes 已省略...]\n"
        + b[-TAIL_BYTES:].decode(errors="replace")
    )


def _read_span(path: Path, start: int, end: int) -> str:
    """读取日志 [start, end) 并按 head/tail 口径截断；大文件只读首尾，不整读。"""
    length = max(0, end - start)
    if length == 0:
        return ""
    try:
        with open(path, "rb") as f:
            if length <= HEAD_BYTES + TAIL_BYTES:
                f.seek(start)
                return f.read(length).decode(errors="replace")
            f.seek(start)
            head = f.read(HEAD_BYTES)
            f.seek(end - TAIL_BYTES)
            tail = f.read(TAIL_BYTES)
    except OSError:
        return ""
    omitted = length - HEAD_BYTES - TAIL_BYTES
    return (
        head.decode(errors="replace")
        + f"\n[...{omitted} bytes 已省略...]\n"
        + tail.decode(errors="replace")
    )


def _new_job_id() -> str:
    """时间有序的 job id：j_ + 48bit 毫秒时间戳 + 80bit 随机（Crockford base32，26 位）。"""
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    value = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    out = []
    for _ in range(26):
        out.append(alphabet[value & 31])
        value >>= 5
    return "j_" + "".join(reversed(out))


class SessionBusy(ToolError):
    """会话正在跑另一个 job。"""

    def __init__(self, job: "Job"):
        super().__init__(f"会话忙：job {job.id} 仍在运行，请先 wait 或 kill")
        self.job = job


@dataclass
class Job:
    id: str
    command: str
    log_path: Path
    state_base: Path
    timeout: float | None
    status: str = STATUS_RUNNING
    exit_code: int | None = None
    kill_reason: str | None = None      # "timeout" | "kill:INT" | "restart" ...
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    process: asyncio.subprocess.Process | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    _kill_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def finished(self) -> bool:
        return self.done.is_set()

    def log_size(self) -> int:
        try:
            return self.log_path.stat().st_size
        except OSError:
            return 0

    def read(self, start: int, end: int | None = None) -> tuple[str, int]:
        """读 [start, end or size)，返回 (截断后的文本, 新 cursor)。"""
        size = self.log_size()
        end = size if end is None else min(end, size)
        start = min(max(start, 0), end)
        return _read_span(self.log_path, start, end), end


class JobSession:
    """持久会话：串行跑 job，跨 job 传递 cwd / 导出环境。"""

    def __init__(self, cwd: str | None = None, env: dict[str, str] | None = None):
        self._initial_cwd = cwd or os.getcwd()
        self._initial_env = dict(env if env is not None else os.environ)
        self.cwd = self._initial_cwd
        self.env = dict(self._initial_env)
        self.jobs: dict[str, Job] = {}
        self.current: Job | None = None
        self._notes: list[str] = []
        JOB_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 提交
    async def start_job(self, command: str, timeout: float | None = None) -> Job:
        """启动一个 job；会话忙则抛 SessionBusy。"""
        if not command:
            raise ToolError("未提供命令。")
        if timeout is not None and timeout <= 0:
            raise ToolError("timeout 必须大于 0。")
        if self.current is not None and not self.current.finished:
            raise SessionBusy(self.current)

        job_id = _new_job_id()
        base = JOB_DIR / job_id
        job = Job(
            id=job_id,
            command=command,
            log_path=base.with_suffix(".log"),
            state_base=base,
            timeout=timeout,
        )
        base.with_suffix(".sh").write_text(command + "\n")

        cwd = self.cwd
        if not os.path.isdir(cwd):
            self._notes.append(f"⚠️ 上次工作目录 {cwd} 已不存在，已回退到 {self._initial_cwd}")
            cwd = self.cwd = self._initial_cwd

        log_fd = os.open(job.log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            job.process = await asyncio.create_subprocess_exec(
                "/bin/bash", "-c", _WRAPPER, "_", str(base),
                cwd=cwd,
                env=self.env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_fd,
                stderr=log_fd,
                start_new_session=True,  # 独立进程组：kill 可整组回收
            )
        finally:
            os.close(log_fd)

        self.current = job
        self.jobs[job_id] = job
        self._prune()
        asyncio.get_running_loop().create_task(self._supervise(job))
        return job

    async def _supervise(self, job: Job) -> None:
        proc = job.process
        assert proc is not None
        try:
            if job.timeout is None:
                await proc.wait()
            else:
                try:
                    await asyncio.wait_for(proc.wait(), job.timeout)
                except asyncio.TimeoutError:
                    await self._terminate(job, "timeout")
                    await proc.wait()
        finally:
            rc = proc.returncode
            # 信号致死按 shell 惯例记 128+N（SIGINT→130，SIGKILL→137）
            job.exit_code = 128 - rc if rc is not None and rc < 0 else rc
            job.status = STATUS_KILLED if job.kill_reason else STATUS_EXITED
            job.finished_at = time.time()
            self._load_state(job)
            job.done.set()
            if job.kill_reason:
                # 被终止的 job：包装 bash 退出不代表进程组已清空（非交互 shell 的后台
                # 子进程默认忽略 SIGINT），宽限后 SIGKILL 扫尾整个组
                asyncio.get_running_loop().create_task(self._sweep_group(job))

    async def _sweep_group(self, job: Job) -> None:
        if not self._group_alive(job):
            return
        await asyncio.sleep(KILL_GRACE)
        try:
            os.killpg(job.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    @staticmethod
    def _group_alive(job: Job) -> bool:
        try:
            os.killpg(job.process.pid, 0)
            return True
        except ProcessLookupError:
            return False

    async def _terminate(self, job: Job, reason: str) -> None:
        """SIGINT 整个进程组，宽限后 SIGKILL。"""
        proc = job.process
        assert proc is not None
        job.kill_reason = job.kill_reason or reason
        self._signal_group(job, signal.SIGINT)
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), KILL_GRACE)
        except asyncio.TimeoutError:
            self._signal_group(job, signal.SIGKILL)

    @staticmethod
    def _signal_group(job: Job, sig: signal.Signals) -> None:
        proc = job.process
        if proc is None or proc.returncode is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass

    def _load_state(self, job: Job) -> None:
        """job 结束后读取其落盘的 cwd / env 作为下一个 job 的起点。"""
        try:
            cwd = job.state_base.with_suffix(".cwd").read_text().strip()
            if cwd:
                self.cwd = cwd
        except OSError:
            pass
        try:
            raw = job.state_base.with_suffix(".env").read_bytes()
        except OSError:
            return
        if not raw:
            return
        env: dict[str, str] = {}
        for item in raw.split(b"\0"):
            if not item or b"=" not in item:
                continue
            k, v = item.decode(errors="replace").split("=", 1)
            if k in _ENV_SKIP:
                continue
            env[k] = v
        if env:
            self.env = env

    def _prune(self) -> None:
        if len(self.jobs) <= MAX_JOB_RECORDS:
            return
        for jid in list(self.jobs):
            if len(self.jobs) <= MAX_JOB_RECORDS:
                break
            j = self.jobs[jid]
            if j.finished and j is not self.current:
                del self.jobs[jid]

    # ------------------------------------------------------------------ 查询/控制
    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise ToolError(f"未知 job：{job_id}")
        return job

    @staticmethod
    async def wait(job: Job, wait: float) -> None:
        """最多等 wait 秒直到 job 结束。"""
        if job.finished or wait <= 0:
            return
        try:
            await asyncio.wait_for(job.done.wait(), wait)
        except asyncio.TimeoutError:
            pass

    async def kill(self, job: Job, sig: str = "INT", reason: str | None = None) -> None:
        """向 job 的进程组发信号；已结束的 job 无操作。"""
        if job.finished:
            return
        try:
            signum = _SIGNALS[sig.upper()]
        except KeyError:
            raise ToolError(f"不支持的信号：{sig}（可选 INT / TERM / KILL）")
        async with job._kill_lock:
            job.kill_reason = job.kill_reason or reason or f"kill:{sig.upper()}"
            self._signal_group(job, signum)

    async def reset(self) -> None:
        """kill 当前 job（SIGKILL），cwd / env 恢复到会话初始值。"""
        if self.current is not None and not self.current.finished:
            self.current.kill_reason = self.current.kill_reason or "restart"
            self._signal_group(self.current, signal.SIGKILL)
            await self.current.done.wait()
        self.cwd = self._initial_cwd
        self.env = dict(self._initial_env)
        self.current = None

    def pop_notes(self) -> str | None:
        if not self._notes:
            return None
        notes, self._notes = self._notes, []
        return "\n".join(notes)


class BashTool:
    """终端工具：job 契约（submit / wait / kill）+ 旧契约（execute / execute_stream）。"""

    def __init__(self):
        self._session: JobSession | None = None

    @property
    def session(self) -> JobSession:
        if self._session is None:
            self._session = JobSession()
        return self._session

    # ------------------------------------------------------------------ job 契约
    async def submit(self, command: str, wait: float = DEFAULT_WAIT, timeout: float | None = None) -> Job:
        """提交命令并最多等 wait 秒（上限 MAX_WAIT）。会话忙抛 SessionBusy。"""
        job = await self.session.start_job(command, timeout=timeout)
        await self.session.wait(job, min(max(wait, 0.0), MAX_WAIT))
        return job

    def get_job(self, job_id: str) -> Job:
        return self.session.get(job_id)

    async def wait(self, job_id: str, wait: float = DEFAULT_WAIT) -> Job:
        job = self.session.get(job_id)
        await self.session.wait(job, min(max(wait, 0.0), MAX_WAIT))
        return job

    async def kill(self, job_id: str, sig: str = "INT") -> Job:
        job = self.session.get(job_id)
        await self.session.kill(job, sig)
        # 给进程一点时间退出，让 kill 响应里尽量直接带 killed 终态
        await self.session.wait(job, 1.0)
        return job

    def pop_notes(self) -> str | None:
        return self.session.pop_notes()

    # ------------------------------------------------------------------ 旧契约
    async def _start_legacy(self, command: str, timeout: float | None) -> tuple[Job, float]:
        effective = DEFAULT_TIMEOUT if timeout is None else min(timeout, MAX_TIMEOUT)
        session = self.session
        # 旧调用方可能并发提交：排队等待前一个 job 结束（与旧版加锁串行行为一致）
        while session.current is not None and not session.current.finished:
            await session.current.done.wait()
        job = await session.start_job(command, timeout=effective)
        return job, effective

    async def execute_job(self, command: str, timeout: float | None = None) -> tuple[Job, ToolResult]:
        """旧契约：阻塞至命令结束（默认 30s、最大 300s 超时），返回 (Job, ToolResult)。"""
        job, effective = await self._start_legacy(command, timeout)
        await job.done.wait()
        output, _ = job.read(0)
        if job.kill_reason == "timeout":
            notice = f"[命令已超时 ({effective:g}s)，进程已终止，session 继续可用]"
            output = f"{notice}\n{output}".strip()
        return job, CLIResult(output=output, error="", system=self.pop_notes())

    async def execute(self, command: str, timeout: float | None = None) -> ToolResult:
        """旧契约：阻塞至命令结束（默认 30s、最大 300s 超时），返回 ToolResult。"""
        _, result = await self.execute_job(command, timeout)
        return result

    def close(self) -> None:
        """服务关闭：SIGKILL 仍在跑的 job 进程组。"""
        if self._session is None:
            return
        job = self._session.current
        if job is not None and not job.finished:
            job.kill_reason = job.kill_reason or "shutdown"
            JobSession._signal_group(job, signal.SIGKILL)

    async def execute_stream(self, command: str, timeout: float | None = None):
        """旧契约 SSE：按行推送日志增量；事件 {"type": "stdout"|"stderr"|"done"}。"""
        job, effective = await self._start_legacy(command, timeout)
        note = self.pop_notes()
        if note:
            yield {"type": "stderr", "chunk": note}
        cursor = 0
        pending = b""
        while True:
            finished = job.finished
            size = job.log_size()
            if size > cursor:
                with open(job.log_path, "rb") as f:
                    f.seek(cursor)
                    pending += f.read(size - cursor)
                cursor = size
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    yield {"type": "stdout", "chunk": line.decode(errors="replace") + "\n"}
            if finished:
                break
            await self.session.wait(job, 0.2)
        if pending:
            yield {"type": "stdout", "chunk": pending.decode(errors="replace")}
        if job.kill_reason == "timeout":
            yield {"type": "stderr", "chunk": f"[命令超时 ({effective:g}s)]"}
        yield {"type": "done"}

    async def restart(self) -> ToolResult:
        """重启终端会话：kill 当前 job，cwd / env 恢复初始值。"""
        await self.session.reset()
        return ToolResult(system="终端会话已重启。")
