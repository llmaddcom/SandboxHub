"""
终端工具模块 - tmux 会话内的 job 化命令执行（issue #30 → tmux 化，承接 createrole#367）。

模型：
- 容器内跑一个 tmux server；**每个对话会话一个 tmux session**（调用方经 ``session``
  字段指定，缺省 ``default``），互不可见。
- 每条命令是一个 job = 该 session 里的一个 tmux window（窗口名 = job_id）。命令经
  ``script -q -f -e`` 在 PTY 里运行：stdout/stderr 与终端所见一致、逐字节写
  ``/tmp/cr-jobs/<job_id>.log``（模型可 tail/grep），交互式程序（REPL、要输入的安装向导）
  可用 ``tmux send-keys -t <job_id>`` 喂输入、``tmux capture-pane -p -t <job_id>`` 看屏幕。
- 「持久会话」= 跨 job 传递的 cwd + 导出环境（按 tmux session 各自维护）：job 结束时
  （EXIT trap）把 ``pwd`` 与 ``env -0`` 落盘，同 session 的下一个 job 以此为起点。
  ``cd``/``export``/``source venv`` 因而跨调用保留；shell 函数/别名/未导出变量不跨越。
- 多 job 并行：同一容器同时可跑多个 job（不再 409）。活跃窗口总数超过
  ``MAX_LIVE_WINDOWS`` 时按最近活动时间淘汰最旧的，最近 ``PROTECT_RECENT`` 个不动
  （对标 codex unified exec 的 64 / 8）。
- 完成判定：``remain-on-exit`` 下轮询 ``pane_dead`` / ``pane_dead_status``（= 命令退出码，
  信号致死为 128+N）；结束后窗口即销毁，日志保留。
- ``timeout``：无默认、无上限。到期先 SIGINT，宽限后 SIGKILL 整个进程组。
- ``wait``：调用方单次最多等待秒数（默认 30，上限 120），到点先返回 running。
- 兼容旧形态：``BashTool.execute(command, timeout)`` 阻塞至结束（默认 30s、最大 300s），
  返回 ToolResult；``execute_stream`` 按行 SSE。
"""

import asyncio
import os
import re
import secrets
import shlex
import signal
import subprocess
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

# ---- tmux ----
TMUX_SOCKET = os.getenv("CR_TMUX_SOCKET") or None   # None = 默认 socket（容器内 tmux ls 直接可见）
DEFAULT_SESSION = "default"
MAX_LIVE_WINDOWS = 64     # 整个 tmux server 的活跃窗口上限（对标 codex MAX_UNIFIED_EXEC_PROCESSES）
PROTECT_RECENT = 8        # 淘汰时保护最近活动的窗口数
POLL_INTERVAL = 0.1       # 轮询 pane 状态的间隔
DEAD_STATUS_TICKS = 10    # pane 已死但退出码未到时最多再等的轮询次数
PANE_COLUMNS, PANE_LINES = 200, 50

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

# 不跨 job 传递的环境变量：shell 自行维护的、tmux/script 按窗口注入的
_ENV_SKIP = {"_", "PWD", "OLDPWD", "SHLVL", "TMUX", "TMUX_PANE", "LINES", "COLUMNS"}
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# job 包装脚本（经 script 在 PTY 里由 bash 执行）：$1 = 状态文件基名。
# 先落 pid（kill 用），载入上一 job 的导出环境，EXIT trap 保证 exit / 报错 / SIGINT 后
# 仍能保存 cwd / env 与退出码（.rc）。退出码以 .rc 为准：tmux 3.2a 的 pane_dead_status
# 会偶发永久为空，只作兜底（SIGKILL 致死无 trap 时）。INT/TERM 显式 exit 130/143：
# 否则 bash 被信号打断后 EXIT trap 里的 $? 是 0，退出码会失真。
_WRAPPER = """\
__cr_state="$1"; set --
echo $$ > "$__cr_state.pid"
if [ -s "$__cr_state.envin" ]; then
  while IFS= read -r -d '' __cr_kv; do export -- "$__cr_kv" 2>/dev/null; done < "$__cr_state.envin"
fi
unset __cr_kv
__cr_save() {
  __cr_rc=$?
  pwd -P > "$__cr_state.cwd" 2>/dev/null; env -0 > "$__cr_state.env" 2>/dev/null
  echo "$__cr_rc" > "$__cr_state.rc"
}
trap __cr_save EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
. "$__cr_state.sh"
"""

# script(1) 在日志首尾写的标记行（util-linux 2.37/2.38 均如此，-q 只静默终端）；读日志时剥离
_SCRIPT_HEADER = b"Script started on "
_SCRIPT_FOOTER_RE = re.compile(rb"\n?Script done on [^\n]*\n?$")
# 终端转义序列（CSI / OSC / 其余 ESC 序列）：TERM=dumb 下程序基本不发，兜底清理
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def _clean_tty(text: str) -> str:
    """PTY 输出 → 模型可读文本：去转义序列、CRLF→LF、``\\r`` 覆盖只留最后一段（进度条）。"""
    text = _ANSI_RE.sub("", text).replace("\r\n", "\n")
    if "\r" in text:
        text = "\n".join(seg.rsplit("\r", 1)[-1] for seg in text.split("\n"))
    return text


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


def session_name(raw: str | None) -> str:
    """调用方的会话标识 → tmux session 名（tmux 不允许 ``.`` ``:``，其余非字母数字统一成 ``_``）。"""
    if not raw:
        return DEFAULT_SESSION
    name = re.sub(r"[^A-Za-z0-9_-]", "_", raw.strip())[:48]
    return name or DEFAULT_SESSION


@dataclass
class Job:
    id: str
    command: str
    session: str
    log_path: Path
    state_base: Path
    timeout: float | None
    pane_id: str = ""
    status: str = STATUS_RUNNING
    exit_code: int | None = None
    kill_reason: str | None = None      # "timeout" | "kill:INT" | "restart" | "evicted" | "window_closed"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    dead_ticks: int = 0
    sigkilled: bool = False             # 我方已对进程组发过 SIGKILL（退出码兜底 137）
    done: asyncio.Event = field(default_factory=asyncio.Event)
    _kill_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def finished(self) -> bool:
        return self.done.is_set()

    @property
    def pid(self) -> int | None:
        """包装 shell 的 pid（job 起步后由包装脚本落盘；极早期可能还没有）。"""
        return self._read_int(".pid")

    @property
    def trap_rc(self) -> int | None:
        """包装脚本 EXIT trap 落盘的退出码；被 SIGKILL 直接打死时没有。"""
        return self._read_int(".rc")

    def _read_int(self, suffix: str) -> int | None:
        try:
            text = self.state_base.with_suffix(suffix).read_text().strip()
        except OSError:
            return None
        try:
            return int(text)
        except ValueError:
            return None

    def log_size(self) -> int:
        try:
            return self.log_path.stat().st_size
        except OSError:
            return 0

    def _raw_span(self, start: int, end: int) -> bytes:
        try:
            with open(self.log_path, "rb") as f:
                f.seek(start)
                return f.read(max(0, end - start))
        except OSError:
            return b""

    def read(self, start: int, end: int | None = None) -> tuple[str, int]:
        """读 [start, end or size)，返回 (清洗+截断后的文本, 新 cursor)。

        cursor 是日志文件的原始字节偏移；script 的首尾标记行只在读到文件头/已结束的文件尾
        时剥离，不影响偏移语义。
        """
        size = self.log_size()
        end = size if end is None else min(end, size)
        start = min(max(start, 0), end)
        if start == 0 and end > 0:
            head = self._raw_span(0, min(end, 512))
            if head.startswith(_SCRIPT_HEADER):
                nl = head.find(b"\n")
                if nl >= 0:
                    start = min(nl + 1, end)
        text_end = end
        if end == size and end - start > 0:
            tail = self._raw_span(max(start, end - 256), end)
            m = _SCRIPT_FOOTER_RE.search(tail)
            if m:
                text_end = end - (len(tail) - m.start())
                if not self.finished:
                    # 尾标记刚写下、pane 尚未判死：先扣住不回传，cursor 停在标记前，
                    # 结束后的下一次读再整体剥离（避免增量读把标记行漏给调用方）
                    end = text_end
        return _clean_tty(_read_span(self.log_path, start, text_end)), end


@dataclass
class _SessionState:
    """一个 tmux session 的跨 job 状态：cwd / 导出环境 / 待带回的提示。"""

    name: str
    initial_cwd: str
    initial_env: dict[str, str]
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cwd = self.cwd or self.initial_cwd
        self.env = self.env or dict(self.initial_env)

    def reset(self) -> None:
        self.cwd = self.initial_cwd
        self.env = dict(self.initial_env)


class Tmux:
    """tmux 命令行薄封装（异步 + 关闭时的同步版）。"""

    def __init__(self, socket: str | None = None):
        self.socket = socket if socket is not None else TMUX_SOCKET

    def _argv(self, *args: str) -> list[str]:
        base = ["tmux"] + (["-S", self.socket] if self.socket else [])
        return base + list(args)

    async def run(self, *args: str) -> str:
        # 走线程里的同步 subprocess 而非 asyncio 子进程：tmux 命令都是毫秒级短调用，且
        # asyncio 子进程在调用协程被取消（事件循环收尾）时会把 loop 关闭卡死。
        proc = await asyncio.to_thread(
            subprocess.run, self._argv(*args),
            stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=False,
        )
        if proc.returncode != 0:
            raise ToolError(f"tmux {args[0]} 失败: {proc.stderr.decode(errors='replace').strip()}")
        return proc.stdout.decode(errors="replace")

    async def ok(self, *args: str) -> bool:
        try:
            await self.run(*args)
            return True
        except ToolError:
            return False

    def run_sync(self, *args: str) -> None:
        subprocess.run(self._argv(*args), stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    async def list_panes(self) -> list[dict]:
        """server 上全部 pane：pane_id / dead / dead_status / activity / session / window。"""
        try:
            out = await self.run(
                "list-panes", "-a", "-F",
                "#{pane_id}\t#{pane_dead}\t#{pane_dead_status}\t#{window_activity}\t"
                "#{session_name}\t#{window_name}",
            )
        except ToolError:
            return []   # server 未起 / 已退出 = 没有 pane
        panes = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 6:
                continue
            pane_id, dead, dead_status, activity, sess, win = parts[:6]
            panes.append({
                "pane_id": pane_id,
                "dead": dead == "1",
                "dead_status": int(dead_status) if dead_status.lstrip("-").isdigit() else None,
                "activity": int(activity) if activity.isdigit() else 0,
                "session": sess,
                "window": win,
            })
        return panes


class JobSession:
    """tmux 化的 job 会话：多 job 并行，按 tmux session 各自传递 cwd / 导出环境。"""

    def __init__(self, cwd: str | None = None, env: dict[str, str] | None = None,
                 tmux: Tmux | None = None):
        self._initial_cwd = cwd or os.getcwd()
        self._initial_env = {
            k: v for k, v in (env if env is not None else os.environ).items()
            if k not in _ENV_SKIP and _ENV_NAME_RE.match(k)
        }
        self.tmux = tmux or Tmux()
        self.states: dict[str, _SessionState] = {}
        self.jobs: dict[str, Job] = {}
        self._supervisor: asyncio.Task | None = None
        JOB_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 会话状态
    def state(self, session: str | None = None) -> _SessionState:
        name = session_name(session)
        st = self.states.get(name)
        if st is None:
            st = self.states[name] = _SessionState(
                name=name, initial_cwd=self._initial_cwd, initial_env=self._initial_env
            )
        return st

    @property
    def running(self) -> list[Job]:
        return [j for j in self.jobs.values() if not j.finished]

    # ------------------------------------------------------------------ 提交
    async def start_job(self, command: str, timeout: float | None = None,
                        session: str | None = None) -> Job:
        """在该 session 的 tmux 会话里开一个窗口跑命令；返回 running 的 job。"""
        if not command:
            raise ToolError("未提供命令。")
        if timeout is not None and timeout <= 0:
            raise ToolError("timeout 必须大于 0。")
        st = self.state(session)

        job_id = _new_job_id()
        base = JOB_DIR / job_id
        job = Job(
            id=job_id,
            command=command,
            session=st.name,
            log_path=base.with_suffix(".log"),
            state_base=base,
            timeout=timeout,
        )
        base.with_suffix(".sh").write_text(command + "\n")
        base.with_suffix(".run.sh").write_text(_WRAPPER)
        base.with_suffix(".envin").write_bytes(
            b"".join(f"{k}={v}".encode(errors="replace") + b"\0" for k, v in st.env.items())
        )

        cwd = st.cwd
        if not os.path.isdir(cwd):
            st.notes.append(f"⚠️ 上次工作目录 {cwd} 已不存在，已回退到 {self._initial_cwd}")
            cwd = st.cwd = self._initial_cwd

        await self._reap_windows()
        inner = (
            f"/bin/bash --noprofile --norc {shlex.quote(str(base.with_suffix('.run.sh')))} "
            f"{shlex.quote(str(base))}"
        )
        window_cmd = f"exec script -q -f -e -c {shlex.quote(inner)} {shlex.quote(str(job.log_path))}"
        job.pane_id = await self._open_window(st.name, job_id, cwd, window_cmd)

        self.jobs[job_id] = job
        self._prune()
        self._ensure_supervisor()
        return job

    async def _open_window(self, session: str, name: str, cwd: str, command: str) -> str:
        """session 存在则 new-window，否则 new-session（顺带套 server 选项）；返回 pane_id。"""
        if await self.tmux.ok("has-session", "-t", f"={session}"):
            out = await self.tmux.run(
                "new-window", "-d", "-P", "-F", "#{pane_id}", "-t", f"={session}:",
                "-n", name, "-c", cwd, command,
            )
        else:
            out = await self.tmux.run(
                "new-session", "-d", "-P", "-F", "#{pane_id}", "-s", session,
                "-x", str(PANE_COLUMNS), "-y", str(PANE_LINES), "-n", name, "-c", cwd, command,
            )
            for opt, val in (("remain-on-exit", "on"), ("history-limit", "50000"),
                             ("exit-empty", "off"), ("status", "off")):
                await self.tmux.ok("set-option", "-g", opt, val)
        return out.strip()

    async def _reap_windows(self) -> None:
        """活跃窗口达上限时淘汰最旧的（按最近活动），最近 PROTECT_RECENT 个不动。"""
        panes = await self.tmux.list_panes()
        live = sorted((p for p in panes if not p["dead"]), key=lambda p: p["activity"])
        excess = len(live) - MAX_LIVE_WINDOWS + 1
        if excess <= 0:
            return
        candidates = live[: max(0, len(live) - PROTECT_RECENT)]
        for pane in candidates[:excess]:
            job = self._job_by_pane(pane["pane_id"])
            if job is not None and not job.finished:
                job.kill_reason = job.kill_reason or "evicted"
                self._signal_group(job, signal.SIGKILL)
            await self.tmux.ok("kill-pane", "-t", pane["pane_id"])

    def _job_by_pane(self, pane_id: str) -> Job | None:
        for job in self.jobs.values():
            if job.pane_id == pane_id:
                return job
        return None

    # ------------------------------------------------------------------ 监督
    def _ensure_supervisor(self) -> None:
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = asyncio.get_running_loop().create_task(self._supervise())

    async def _supervise(self) -> None:
        """单循环轮询全部 running job：pane 死亡即结算；timeout 到期即终止。"""
        while self.running:
            await asyncio.sleep(POLL_INTERVAL)
            panes = {p["pane_id"]: p for p in await self.tmux.list_panes()}
            now = time.time()
            for job in self.running:
                pane = panes.get(job.pane_id)
                if pane is None:
                    # 窗口没了（模型 tmux kill-window / 被淘汰 / server 退出）
                    self._settle(job, None, job.kill_reason or "window_closed")
                elif pane["dead"]:
                    rc = job.trap_rc
                    if rc is None:
                        # 无 trap 退出码（SIGKILL 致死）：等几拍 pane_dead_status；仍无则按
                        # 是否由我方 SIGKILL 兜底为 137，否则如实为 None
                        job.dead_ticks += 1
                        rc = pane["dead_status"]
                        if rc is None and job.dead_ticks < DEAD_STATUS_TICKS:
                            continue
                        if rc is None and job.sigkilled:
                            rc = 137
                    # 先销毁窗口再结算：调用方拿到终态时窗口已不在（日志保留）
                    await self.tmux.ok("kill-pane", "-t", job.pane_id)
                    self._settle(job, rc, job.kill_reason)
                elif job.timeout is not None and now - job.started_at >= job.timeout:
                    asyncio.get_running_loop().create_task(self._terminate(job, "timeout"))

    def _settle(self, job: Job, exit_code: int | None, kill_reason: str | None) -> None:
        job.exit_code = exit_code
        job.kill_reason = kill_reason
        job.status = STATUS_KILLED if kill_reason else STATUS_EXITED
        job.finished_at = time.time()
        self._load_state(job)
        job.done.set()
        if kill_reason:
            # 被终止的 job：包装 bash 退出不代表进程组已清空（非交互 shell 的后台子进程
            # 默认忽略 SIGINT），宽限后 SIGKILL 扫尾整个组
            asyncio.get_running_loop().create_task(self._sweep_group(job))

    async def _sweep_group(self, job: Job) -> None:
        if not self._group_alive(job):
            return
        await asyncio.sleep(KILL_GRACE)
        self._signal_group(job, signal.SIGKILL, force=True)

    @staticmethod
    def _group_alive(job: Job) -> bool:
        pid = job.pid
        if pid is None:
            return False
        try:
            os.killpg(os.getpgid(pid), 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    async def _terminate(self, job: Job, reason: str) -> None:
        """SIGINT 整个进程组，宽限后 SIGKILL。"""
        if job.finished:
            return
        job.kill_reason = job.kill_reason or reason
        await self._signal(job, signal.SIGINT)
        try:
            await asyncio.wait_for(asyncio.shield(job.done.wait()), KILL_GRACE)
        except asyncio.TimeoutError:
            await self._signal(job, signal.SIGKILL)

    async def _signal(self, job: Job, sig: signal.Signals) -> None:
        """向 job 进程组发信号；pid 尚未落盘（刚起步）时退化为 tmux 侧动作。"""
        if job.finished:
            return
        if self._signal_group(job, sig):
            return
        if sig is signal.SIGINT:
            await self.tmux.ok("send-keys", "-t", job.pane_id, "C-c")
        else:
            job.sigkilled = True
            await self.tmux.ok("kill-pane", "-t", job.pane_id)

    @staticmethod
    def _signal_group(job: Job, sig: signal.Signals, force: bool = False) -> bool:
        """向 job 进程组发信号。返回 False 表示 pid 还没落盘、没发出去。"""
        if job.finished and not force:
            return True
        pid = job.pid
        if pid is None:
            return False
        if sig is signal.SIGKILL:
            job.sigkilled = True
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError):
            pass
        return True

    def _load_state(self, job: Job) -> None:
        """job 结束后读取其落盘的 cwd / env 作为同 session 下一个 job 的起点。"""
        st = self.state(job.session)
        try:
            cwd = job.state_base.with_suffix(".cwd").read_text().strip()
            if cwd:
                st.cwd = cwd
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
            if k in _ENV_SKIP or not _ENV_NAME_RE.match(k):
                continue
            env[k] = v
        if env:
            st.env = env

    def _prune(self) -> None:
        if len(self.jobs) <= MAX_JOB_RECORDS:
            return
        for jid in list(self.jobs):
            if len(self.jobs) <= MAX_JOB_RECORDS:
                break
            if self.jobs[jid].finished:
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
            await self._signal(job, signum)

    async def reset(self, session: str | None = None) -> None:
        """kill 该 session 仍在跑的 job（SIGKILL）并销毁其 tmux 会话，cwd / env 复位。"""
        st = self.state(session)
        mine = [j for j in self.running if j.session == st.name]
        for job in mine:
            job.kill_reason = job.kill_reason or "restart"
            self._signal_group(job, signal.SIGKILL)
        await self.tmux.ok("kill-session", "-t", f"={st.name}")
        for job in mine:
            await self.wait(job, KILL_GRACE)
        st.reset()

    def pop_notes(self, session: str | None = None) -> str | None:
        st = self.state(session)
        if not st.notes:
            return None
        notes, st.notes = st.notes, []
        return "\n".join(notes)

    def close(self) -> None:
        """服务关闭：SIGKILL 仍在跑的 job 进程组并关掉 tmux server。"""
        for job in self.running:
            job.kill_reason = job.kill_reason or "shutdown"
            self._signal_group(job, signal.SIGKILL)
        self.tmux.run_sync("kill-server")


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
    async def submit(self, command: str, wait: float = DEFAULT_WAIT, timeout: float | None = None,
                     session: str | None = None) -> Job:
        """提交命令并最多等 wait 秒（上限 MAX_WAIT）。"""
        job = await self.session.start_job(command, timeout=timeout, session=session)
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

    def pop_notes(self, session: str | None = None) -> str | None:
        return self.session.pop_notes(session)

    # ------------------------------------------------------------------ 旧契约
    async def _start_legacy(self, command: str, timeout: float | None,
                            session: str | None = None) -> tuple[Job, float]:
        effective = DEFAULT_TIMEOUT if timeout is None else min(timeout, MAX_TIMEOUT)
        job = await self.session.start_job(command, timeout=effective, session=session)
        return job, effective

    async def execute_job(self, command: str, timeout: float | None = None,
                          session: str | None = None) -> tuple[Job, ToolResult]:
        """旧契约：阻塞至命令结束（默认 30s、最大 300s 超时），返回 (Job, ToolResult)。"""
        job, effective = await self._start_legacy(command, timeout, session)
        await job.done.wait()
        output, _ = job.read(0)
        if job.kill_reason == "timeout":
            notice = f"[命令已超时 ({effective:g}s)，进程已终止，session 继续可用]"
            output = f"{notice}\n{output}".strip()
        return job, CLIResult(output=output, error="", system=self.pop_notes(session))

    async def execute(self, command: str, timeout: float | None = None) -> ToolResult:
        """旧契约：阻塞至命令结束（默认 30s、最大 300s 超时），返回 ToolResult。"""
        _, result = await self.execute_job(command, timeout)
        return result

    def close(self) -> None:
        """服务关闭：SIGKILL 仍在跑的 job 进程组并关掉 tmux server。"""
        if self._session is not None:
            self._session.close()

    async def execute_stream(self, command: str, timeout: float | None = None,
                             session: str | None = None):
        """旧契约 SSE：按行推送日志增量；事件 {"type": "stdout"|"stderr"|"done"}。"""
        job, effective = await self._start_legacy(command, timeout, session)
        note = self.pop_notes(session)
        if note:
            yield {"type": "stderr", "chunk": note}
        cursor = 0
        pending = ""
        while True:
            finished = job.finished
            text, cursor = job.read(cursor)
            if text:
                pending += text
                *lines, pending = pending.split("\n")
                for line in lines:
                    yield {"type": "stdout", "chunk": line + "\n"}
            if finished:
                break
            await self.session.wait(job, 0.2)
        if pending:
            yield {"type": "stdout", "chunk": pending}
        if job.kill_reason == "timeout":
            yield {"type": "stderr", "chunk": f"[命令超时 ({effective:g}s)]"}
        yield {"type": "done"}

    async def restart(self, session: str | None = None) -> ToolResult:
        """重启终端会话：kill 该会话在跑的 job，cwd / env 恢复初始值。"""
        await self.session.reset(session)
        return ToolResult(system="终端会话已重启。")
