"""
长驻进程工具模块（SandboxHub#52，承接 createrole_com_api#160 执行引擎托管）。

终端 job 是给模型看的（tmux + PTY，stdout/stderr 合并、剥转义、截断）；proc 是给程序用的：
本应用直接以管道持有进程，stdin 可写（背压）、可关闭，stdout / stderr 分开、原样按字节落日志，
调用方按绝对字节偏移续读。用于托管 pi（RPC JSONL）、DSH（ACP JSON-RPC）这类经 stdio 驱动、
一个产品会话一个、可能持续数小时的引擎进程。

- 归属：进程、输出泵、退出监视都在本应用的后台线程里，不属于任何 HTTP 请求，也不绑定
  事件循环；调用方断线、请求被取消都不影响进程。没有存活上限（不套用 job_max_lifetime），
  由调用方回收。应用退出（容器停止）时 SIGKILL 全部进程组。
- 命名：``p_<ULID>`` 标识 + 调用方给的 ``name``。同名的运行中 proc 只能有一个（409）；
  同名的已结束 proc 在新的启动前回收（记录 + 日志）。
- 环境：只有调用方的 ``env``，加上 ``inherit_env`` 点名从本应用环境继承的变量（与 ``env``
  同名时 ``env`` 优先），再加标记 ``CR_SANDBOX_PROC_ID=<proc_id>``。其余一概不继承（尤其
  SANDBOX_API_TOKEN）。标记随环境传给后代，activity 据此把脱离进程组的后代也算作活动。
- 启动前：``reset_dirs`` 里的目录先整个删除（引擎主目录在租户可写的位置，防上一轮预埋文件），
  再写 ``files``（父目录按需创建，临时文件 + 原子替换，按给定权限）。
- 进程组：新会话（start_new_session），信号发给整个进程组。
- 日志：``PROC_DIR/<proc_id>/``，stdout / stderr 各自从 0 计绝对偏移。``log_retain_bytes = 0``
  时每路一个文件（``stdout`` / ``stderr``）；``> 0`` 时每路分段（``stdout.<起始偏移>``），活段写满
  该大小即另起一段、更早的一段删除，只保证最近 ``log_retain_bytes`` 字节可读；游标早于这个
  窗口时从窗口起点返回并标记 truncated。读写都走已打开的 fd，文件被外部删除、改名不影响。
- 结束：主进程退出后等两路输出读尽（后代仍持有管道时最多等 ``DRAIN_GRACE`` 秒），随后状态、
  退出码和两路大小定格（之后的输出丢弃）。我方经 signal / DELETE 发过信号的记 killed，否则
  exited；exit_code 为负表示被该号信号终止。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .base import ToolError
from .bash import JOB_ACTIVITY_ENV, MAX_WAIT, new_ulid, scan_marked

PROC_DIR = Path(os.getenv("CR_PROC_DIR", "/tmp/cr-procs"))
PROC_ACTIVITY_ENV = "CR_SANDBOX_PROC_ID"
_PROC_ACTIVITY_RE = re.compile(rb"(?:^|\x00)CR_SANDBOX_PROC_ID=(p_[0-9A-HJKMNP-TV-Z]{26})(?:\x00|$)")
MOUNTINFO = Path("/proc/self/mountinfo")

DEFAULT_READ_BYTES = 1 << 20   # read 的 max_bytes=0：每路默认最多 1 MiB
MAX_READ_BYTES = 16 << 20      # 每路单次读取的服务端上限（更大的请求按此截取，调用方照常续读）
READ_CHUNK = 64 << 10
POLL_MS = 500                  # 泵 / stdin 写等待的轮询粒度（停止标志在这么久内生效）
DRAIN_GRACE = 2.0              # 主进程退出后等两路输出读尽的上限（后代仍持有管道时）
REMOVE_WAIT = 5.0              # DELETE 运行中的 proc：KILL 后等它结束的上限
NAME_MAX = 256

STATE_RUNNING = "running"
STATE_EXITED = "exited"
STATE_KILLED = "killed"

SIGNALS = {"INT": signal.SIGINT, "TERM": signal.SIGTERM, "HUP": signal.SIGHUP, "KILL": signal.SIGKILL}
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_ENV = frozenset({PROC_ACTIVITY_ENV, JOB_ACTIVITY_ENV})
_MOUNT_ESCAPE_RE = re.compile(r"\\([0-7]{3})")


class ProcError(Exception):
    """接口错误：``status`` = HTTP 状态码，``reason`` = 机器码，``extra`` 并入错误体。"""

    def __init__(self, status: int, reason: str, message: str, **extra) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.message = message
        self.extra = extra


def _invalid(message: str) -> ProcError:
    return ProcError(400, "invalid_request", message)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class FileSpec:
    path: str
    content: bytes
    mode: int = 0o600


@dataclass(frozen=True)
class StartSpec:
    name: str
    argv: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    inherit_env: tuple[str, ...] = ()
    cwd: str | None = None
    files: tuple[FileSpec, ...] = ()
    log_retain_bytes: int = 0
    reset_dirs: tuple[str, ...] = ()


# ── 启动前的校验与准备 ─────────────────────────────────────────────────────────


def _default_cwd() -> str:
    preset = os.getenv("SANDBOX_CWD", "")
    return preset if preset and os.path.isdir(preset) else os.getcwd()


def _check_abs_path(path: str, what: str) -> None:
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\x00" in path
        or path == "/"
        or os.path.normpath(path) != path
    ):
        raise _invalid(f"{what} 须是规范化的绝对路径（不含 .、..、重复或末尾斜杠，不能是 /）：{path!r}")


def _mount_points() -> list[str]:
    try:
        lines = MOUNTINFO.read_text().splitlines()
    except OSError as exc:
        raise _invalid(f"无法读取挂载表，拒绝清空目录：{exc}") from exc
    points = []
    for line in lines:
        fields = line.split(" ")
        if len(fields) > 4:
            points.append(_MOUNT_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 8)), fields[4]))
    return points


def _check_reset_dir(path: str, mounts: list[str]) -> None:
    """可清空的目录：非顶层、不是也不包含挂载点（/workspace/* 的 bind、角色目录）、上级没有符号链接。"""
    _check_abs_path(path, "reset_dirs")
    if path.count("/") < 2:
        raise _invalid(f"reset_dirs 不能是顶层目录：{path}")
    for point in mounts:
        if point == path or point.startswith(path + "/"):
            raise _invalid(f"reset_dirs 不能是挂载点或包含挂载点：{path}（{point}）")
    parent = os.path.dirname(path)
    if os.path.realpath(parent) != parent:
        raise _invalid(f"reset_dirs 的上级路径含符号链接：{path}")


def _check_spec(spec: StartSpec) -> str:
    """全部校验在任何副作用之前完成；返回生效的 cwd。"""
    if not 1 <= len(spec.name) <= NAME_MAX or any(ord(c) < 32 or ord(c) == 127 for c in spec.name):
        raise _invalid(f"name 须为 1–{NAME_MAX} 个字符且不含控制字符")
    if not spec.argv or not spec.argv[0]:
        raise _invalid("argv 不能为空")
    if any("\x00" in arg for arg in spec.argv):
        raise _invalid("argv 不能含 NUL")
    for name in (*spec.env, *spec.inherit_env):
        if not _ENV_NAME_RE.match(name):
            raise _invalid(f"环境变量名非法：{name!r}")
        if name in _RESERVED_ENV:
            raise _invalid(f"{name} 由服务端设置，不能由调用方传入")
    if any("\x00" in value for value in spec.env.values()):
        raise _invalid("环境变量值不能含 NUL")
    if spec.log_retain_bytes < 0:
        raise _invalid("log_retain_bytes 不能为负")
    cwd = spec.cwd or _default_cwd()
    if not os.path.isabs(cwd) or "\x00" in cwd:
        raise _invalid(f"cwd 须是绝对路径：{cwd!r}")
    # cwd 可以是本次写入的文件所在的目录（如每次重建的引擎主目录）：写完文件后再核对一次。
    if not os.path.isdir(cwd) and not _created_by_files(cwd, spec.files):
        raise _invalid(f"cwd 不存在或不是目录：{cwd}")
    for item in spec.files:
        _check_abs_path(item.path, "files.path")
        if not 0 <= item.mode <= 0o7777:
            raise _invalid(f"files.mode 越界：{item.mode}")
    if spec.reset_dirs:
        mounts = _mount_points()
        for path in spec.reset_dirs:
            _check_reset_dir(path, mounts)
    return cwd


def _created_by_files(cwd: str, files) -> bool:
    """cwd 是否会在写入 ``files`` 时作为某个文件的上级目录被建出来。"""
    prefix = os.path.normpath(cwd).rstrip("/") + "/"
    return any(os.path.normpath(item.path).startswith(prefix) for item in files)


def _reset_dir(path: str) -> None:
    """删掉整个目录；该位置若被换成了文件或符号链接，只删它本身，不跟随。"""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def _write_file(item: FileSpec) -> None:
    parent = os.path.dirname(item.path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".cr-proc-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(item.content)
        os.chmod(temporary, item.mode)
        os.replace(temporary, item.path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _build_env(spec: StartSpec, proc_id: str) -> dict[str, str]:
    env = {name: os.environ[name] for name in spec.inherit_env if name in os.environ}
    env.update(spec.env)
    env[PROC_ACTIVITY_ENV] = proc_id
    return env


# ── 输出日志 ───────────────────────────────────────────────────────────────────


class StreamLog:
    """一路输出的追加日志，按绝对字节偏移读写；线程安全。语义见模块说明。"""

    def __init__(self, directory: Path, name: str, retain: int) -> None:
        self.directory = directory
        self.name = name
        self.retain = retain
        self.size = 0          # 自启动以来写出的总字节数（含已按保留量丢弃的部分）
        self.frozen = False
        self._lock = threading.Lock()
        self._segments: list[tuple[int, int]] = []   # (起始偏移, fd)，旧在前
        self._segments.append((0, self._open(0)))

    def _open(self, start: int) -> int:
        filename = f"{self.name}.{start}" if self.retain else self.name
        return os.open(
            self.directory / filename,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_CLOEXEC,
            0o600,
        )

    def append(self, data: bytes) -> bool:
        """写入一块输出；已定格返回 False。写盘失败抛 OSError。"""
        with self._lock:
            if self.frozen:
                return False
            start, fd = self._segments[-1]
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            self.size += len(data)
            if self.retain and self.size - start >= self.retain:
                # 活段已满：另起一段；只留两段即可覆盖最近 retain 字节
                self._segments.append((self.size, self._open(self.size)))
                if len(self._segments) > 2:
                    old_start, old_fd = self._segments.pop(0)
                    os.close(old_fd)
                    with contextlib.suppress(OSError):
                        os.unlink(self.directory / f"{self.name}.{old_start}")
            return True

    def read(self, cursor: int, limit: int) -> tuple[bytes, int, bool]:
        """读 [cursor, cursor + limit) 中仍可读的部分 → (数据, 下次游标, 游标是否早于保留窗口)。"""
        with self._lock:
            if not self._segments:          # 已回收
                return b"", self.size, cursor < self.size
            window = max(self._segments[0][0], self.size - self.retain) if self.retain else 0
            truncated = cursor < window
            start = min(max(cursor, window), self.size)
            end = min(self.size, start + limit)
            chunks = []
            for index, (seg_start, fd) in enumerate(self._segments):
                seg_end = self._segments[index + 1][0] if index + 1 < len(self._segments) else self.size
                lo, hi = max(start, seg_start), min(end, seg_end)
                if lo < hi:
                    chunks.append(os.pread(fd, hi - lo, lo - seg_start))
            return b"".join(chunks), end, truncated

    def freeze(self) -> None:
        with self._lock:
            self.frozen = True

    def close(self) -> None:
        with self._lock:
            self.frozen = True
            for _, fd in self._segments:
                with contextlib.suppress(OSError):
                    os.close(fd)
            self._segments = []


# ── 单个进程 ───────────────────────────────────────────────────────────────────


def _wake(future: asyncio.Future) -> None:
    if not future.done():
        future.set_result(None)


class Proc:
    """一个长驻进程：两路输出泵 + 退出监视（后台线程）+ 串行化的 stdin 写入。"""

    def __init__(self, proc_id: str, name: str, popen: subprocess.Popen, log_dir: Path,
                 stdout: StreamLog, stderr: StreamLog) -> None:
        self.id = proc_id
        self.name = name
        self.popen = popen
        self.pid = popen.pid
        self.log_dir = log_dir
        self.stdout = stdout
        self.stderr = stderr
        self.started_at = time.time()
        self.ended_at: float | None = None
        self.exit_code: int | None = None
        self.state = STATE_RUNNING
        self.stdin_open = True
        self.signaled = False                 # 我方在主进程退出前发过信号 → 结束记 killed
        self._lock = threading.Lock()         # 状态与等待者
        self._stdin_lock = threading.Lock()   # 串行化 stdin 写入 / 关闭
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future]] = []
        self._reaped = threading.Event()      # 主进程已退出（已回收）
        self._stop = threading.Event()        # 泵停止（结束定格后 / 回收时）
        os.set_blocking(popen.stdin.fileno(), False)

    @property
    def running(self) -> bool:
        return self.state == STATE_RUNNING

    def start_threads(self) -> None:
        pumps = [
            threading.Thread(target=self._pump, args=(pipe, log), name=f"cr-proc-{log.name}-{self.id[-6:]}",
                             daemon=True)
            for pipe, log in ((self.popen.stdout, self.stdout), (self.popen.stderr, self.stderr))
        ]
        for pump in pumps:
            pump.start()
        threading.Thread(target=self._watch, args=(pumps,), name=f"cr-proc-wait-{self.id[-6:]}",
                         daemon=True).start()

    # ---- 后台线程 ----
    def _pump(self, pipe, log: StreamLog) -> None:
        fd = pipe.fileno()
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        try:
            while not self._stop.is_set():
                if not poller.poll(POLL_MS):
                    continue
                data = os.read(fd, READ_CHUNK)
                if not data:
                    return
                try:
                    stored = log.append(data)
                except OSError as exc:
                    # 写不进日志就保证不了按游标不丢字节：终止进程，让调用方看到结束而不是静默卡住
                    print(f"[procs] {self.id} {log.name} 写日志失败，终止进程组：{exc}", flush=True)
                    log.freeze()
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(self.pid, signal.SIGKILL)
                    continue
                if stored:
                    self._notify()
        except OSError:
            return
        finally:
            with contextlib.suppress(OSError):
                pipe.close()

    def _watch(self, pumps: list[threading.Thread]) -> None:
        returncode = self.popen.wait()
        self._reaped.set()
        deadline = time.monotonic() + DRAIN_GRACE
        for pump in pumps:
            pump.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            self.stdout.freeze()
            self.stderr.freeze()
            self.exit_code = returncode
            self.ended_at = time.time()
            self.state = STATE_KILLED if self.signaled else STATE_EXITED
        self._stop.set()   # 后代仍持有管道的：不再等，泵在 POLL_MS 内退出并关闭读端
        self._close_stdin()
        self._notify()

    # ---- 等待（事件循环侧）----
    def _notify(self) -> None:
        with self._lock:
            waiters, self._waiters = self._waiters, []
        for loop, future in waiters:
            with contextlib.suppress(RuntimeError):   # 等待方的事件循环已关闭
                loop.call_soon_threadsafe(_wake, future)

    async def wait_until(self, ready: Callable[[], bool], timeout: float) -> None:
        """最多等 ``timeout`` 秒直到 ``ready()``（持状态锁求值）为真；有新输出或状态变化时重判。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            future = loop.create_future()
            with self._lock:
                if ready():
                    return
                self._waiters.append((loop, future))
            try:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return
                await asyncio.wait_for(future, remaining)
            except asyncio.TimeoutError:
                return
            finally:
                with self._lock, contextlib.suppress(ValueError):
                    self._waiters.remove((loop, future))

    # ---- 查询 / 读取 ----
    def info(self) -> dict:
        with self._lock:
            return {
                "proc_id": self.id,
                "name": self.name,
                "pid": self.pid,
                "state": self.state,
                "exit_code": self.exit_code,
                "started_at": _iso(self.started_at),
                "ended_at": _iso(self.ended_at) if self.ended_at is not None else None,
                "stdin_open": self.stdin_open,
                "stdout_size": self.stdout.size,
                "stderr_size": self.stderr.size,
            }

    def has_output_after(self, stdout_cursor: int, stderr_cursor: int) -> bool:
        return self.stdout.size > stdout_cursor or self.stderr.size > stderr_cursor

    def read(self, stdout_cursor: int, stderr_cursor: int, limit: int) -> dict:
        out, out_next, out_cut = self.stdout.read(stdout_cursor, limit)
        err, err_next, err_cut = self.stderr.read(stderr_cursor, limit)
        return {
            "stdout_b64": base64.b64encode(out).decode("ascii"),
            "stdout_cursor": out_next,
            "stderr_b64": base64.b64encode(err).decode("ascii"),
            "stderr_cursor": err_next,
            "truncated": out_cut or err_cut,
            # 状态取在数据之后：返回的大小不小于返回的游标；已结束时大小已定格
            "proc": self.info(),
        }

    # ---- stdin（阻塞，在 IO 线程池里执行）----
    def write_stdin(self, data: bytes, close: bool) -> dict:
        with self._stdin_lock:
            with self._lock:
                running, stdin_open = self.running, self.stdin_open
            if not running or not stdin_open:
                if data:
                    reason = "proc_exited" if not running else "stdin_closed"
                    raise ProcError(409, reason, "进程已结束" if not running else "stdin 已关闭")
                return {"written": 0, "stdin_open": False}   # 重复关闭：幂等
            written = self._write_all(data) if data else 0
            if close:
                self._close_stdin_locked()
            with self._lock:
                return {"written": written, "stdin_open": self.stdin_open}

    def _write_all(self, data: bytes) -> int:
        """全部写进管道才返回（管道满时等子进程读走 = 背压）；进程结束或读端关闭抛 409。"""
        fd = self.popen.stdin.fileno()
        poller = select.poll()
        poller.register(fd, select.POLLOUT)
        view = memoryview(data)
        written = 0
        try:
            while written < len(view):
                if self._reaped.is_set() or self._stop.is_set():   # 已退出 / 正被回收
                    raise ProcError(409, "proc_exited", "进程已结束，stdin 写入中止", written=written)
                try:
                    written += os.write(fd, view[written:])
                except BlockingIOError:
                    poller.poll(POLL_MS)
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._close_stdin_locked()
            exited = self._reaped.wait(1.0)
            raise ProcError(
                409,
                "proc_exited" if exited else "stdin_closed",
                f"{'进程已结束' if exited else '进程关闭了 stdin'}：{exc}",
                written=written,
            ) from exc
        return written

    def _close_stdin(self) -> None:
        with self._stdin_lock:
            self._close_stdin_locked()

    def _close_stdin_locked(self) -> None:
        with self._lock:
            if not self.stdin_open:
                return
            self.stdin_open = False
        with contextlib.suppress(OSError):
            self.popen.stdin.close()

    # ---- 信号 / 回收 ----
    def send_signal(self, sig: signal.Signals) -> None:
        """发给整个进程组；已结束的 proc 无操作。"""
        with self._lock:
            if not self.running:
                return
            if not self._reaped.is_set():
                self.signaled = True
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.pid, sig)

    def kill_quietly(self) -> None:
        if self.running:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.pid, signal.SIGKILL)

    def discard(self) -> None:
        """回收：停泵、关 stdin、释放日志 fd、删日志目录（进程须已结束或已 KILL）。"""
        self._stop.set()
        self._close_stdin()
        self.stdout.close()
        self.stderr.close()
        shutil.rmtree(self.log_dir, ignore_errors=True)

    @classmethod
    def spawn(cls, proc_id: str, spec: StartSpec, cwd: str, env: dict[str, str]) -> "Proc":
        log_dir = PROC_DIR / proc_id
        logs: list[StreamLog] = []
        try:
            PROC_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            log_dir.mkdir(mode=0o700)
            logs = [StreamLog(log_dir, "stdout", spec.log_retain_bytes),
                    StreamLog(log_dir, "stderr", spec.log_retain_bytes)]
        except OSError as exc:
            for log in logs:
                log.close()
            shutil.rmtree(log_dir, ignore_errors=True)
            raise ProcError(500, "start_failed", f"无法创建输出日志：{exc}") from exc
        try:
            popen = subprocess.Popen(
                list(spec.argv),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                start_new_session=True,
                close_fds=True,
                bufsize=0,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            for log in logs:
                log.close()
            shutil.rmtree(log_dir, ignore_errors=True)
            raise _invalid(f"启动失败：{exc}") from exc
        proc = cls(proc_id, spec.name, popen, log_dir, logs[0], logs[1])
        proc.start_threads()
        return proc


# ── 管理器 ─────────────────────────────────────────────────────────────────────


class ProcManager:
    """本应用持有的全部 proc。方法可在事件循环里调用；阻塞动作进专用 IO 线程池。"""

    def __init__(self) -> None:
        self._procs: dict[str, Proc] = {}
        self._lock = threading.Lock()         # 登记表
        self._start_lock = threading.Lock()   # 串行化启动（同名唯一）
        self._starting = 0                    # 已进入启动、尚未登记的 proc 数（计入活动）
        self._pool: ThreadPoolExecutor | None = None
        self.used = False                     # 本应用启动过 proc：activity 才扫描 proc 标记

    def _executor(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="cr-proc-io")
            return self._pool

    async def _run(self, fn: Callable, *args):
        return await asyncio.get_running_loop().run_in_executor(self._executor(), fn, *args)

    # ---- 查询 ----
    def get(self, proc_id: str) -> Proc:
        with self._lock:
            proc = self._procs.get(proc_id)
        if proc is None:
            raise ProcError(404, "proc_not_found", f"未知进程：{proc_id}")
        return proc

    def list(self, name: str | None = None) -> list[dict]:
        with self._lock:
            procs = [p for p in self._procs.values() if name is None or p.name == name]
        return [p.info() for p in sorted(procs, key=lambda p: (p.started_at, p.id))]

    def running_count(self) -> int:
        with self._lock:
            return self._starting + sum(p.running for p in self._procs.values())

    def marked_processes(self) -> int:
        """带 proc 标记的存活进程数（含主进程已结束后仍在跑的后代）。

        从未启动过 proc 时为 0、不扫描。扫描不确定且一个也没找到时：已有运行中的 proc
        则活动已确定（返回 0），否则抛 ToolError（活性未知，调用方按忙处理）。
        """
        if not self.used:
            return 0
        try:
            return len(scan_marked(_PROC_ACTIVITY_RE))
        except ToolError:
            if self.running_count():
                return 0
            raise

    # ---- 启动 ----
    async def start(self, spec: StartSpec) -> dict:
        # 在线程池里跑：调用方断线、请求被取消也会跑完并登记（可按 name 找回）
        return await self._run(self._start, spec)

    def _start(self, spec: StartSpec) -> dict:
        cwd = _check_spec(spec)
        with self._start_lock:
            with self._lock:
                same = [p for p in self._procs.values() if p.name == spec.name]
            for proc in same:
                if proc.running:
                    raise ProcError(409, "proc_exists", f"同名进程仍在运行：{spec.name}", proc_id=proc.id)
            for proc in same:
                self._forget(proc)
            try:
                for path in spec.reset_dirs:
                    _reset_dir(path)
                for item in spec.files:
                    _write_file(item)
            except OSError as exc:
                raise _invalid(f"启动前准备目录 / 文件失败：{exc}") from exc
            if not os.path.isdir(cwd):
                raise _invalid(f"cwd 不存在或不是目录：{cwd}")
            proc_id = new_ulid("p_")
            with self._lock:
                self.used = True
                self._starting += 1
            try:
                proc = Proc.spawn(proc_id, spec, cwd, _build_env(spec, proc_id))
                with self._lock:
                    self._procs[proc_id] = proc
            finally:
                with self._lock:
                    self._starting -= 1
        return proc.info()

    def _forget(self, proc: Proc) -> None:
        with self._lock:
            if self._procs.get(proc.id) is proc:
                del self._procs[proc.id]
        proc.discard()

    # ---- 交互 ----
    async def write_stdin(self, proc_id: str, data: bytes, close: bool) -> dict:
        return await self._run(self.get(proc_id).write_stdin, data, close)

    async def read(self, proc_id: str, stdout_cursor: int, stderr_cursor: int,
                   wait: float, max_bytes: int) -> dict:
        proc = self.get(proc_id)
        wait = min(max(wait, 0.0), MAX_WAIT)
        if wait > 0:
            await proc.wait_until(
                lambda: not proc.running or proc.has_output_after(stdout_cursor, stderr_cursor), wait
            )
        proc = self.get(proc_id)   # 等待期间被回收 → 404
        limit = min(max_bytes or DEFAULT_READ_BYTES, MAX_READ_BYTES)
        return await self._run(proc.read, stdout_cursor, stderr_cursor, limit)

    def signal(self, proc_id: str, name: str) -> None:
        sig = SIGNALS.get(name.strip().upper().removeprefix("SIG"))
        if sig is None:
            raise _invalid(f"不支持的信号：{name!r}（可选 INT / TERM / HUP / KILL）")
        self.get(proc_id).send_signal(sig)

    async def remove(self, proc_id: str) -> None:
        proc = self.get(proc_id)
        if proc.running:
            proc.send_signal(signal.SIGKILL)
            await proc.wait_until(lambda: not proc.running, REMOVE_WAIT)
        await self._run(self._forget, proc)

    def close(self) -> None:
        """服务关闭：SIGKILL 仍在运行的 proc 进程组。"""
        with self._lock:
            procs = list(self._procs.values())
            pool, self._pool = self._pool, None
        for proc in procs:
            proc.kill_quietly()
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
