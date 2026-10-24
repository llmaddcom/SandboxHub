"""v2 按工作区分配的真实环境验收（ADR 0025）：真 Docker、真 cr-fs、真 iptables、真 MinIO。

会在宿主上建临时网络 / 网关 / cr-fs / iptables 链（名字带随机后缀，与在跑的 Hub 互不相干），
结束时全部撤掉。默认跳过；需要显式开启并提供 MinIO 连接：

    SANDBOXHUB_LIVE=1 MINIO_ENDPOINT=... MINIO_ACCESS_KEY=... MINIO_SECRET_KEY=... \
        python -m pytest tests/integration/test_work_area_live.py -s

覆盖：自检全绿（含阳性对照）；实例内无 FUSE / 无挂载能力 / 无存储凭据；只读目录写不进；
存储、宿主端口、其他实例不可达；容器 API 校验每实例 token；工作区跨实例保留；
归档读取不跟随软链接；后台 job 超过最长存活时间被终止；Hub 重启后收养实例。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import subprocess
import uuid
from dataclasses import replace

import httpx
import pytest

from src.config import REPO_ROOT, settings

pytestmark = pytest.mark.skipif(
    os.environ.get("SANDBOXHUB_LIVE") != "1",
    reason="真实环境验收：设置 SANDBOXHUB_LIVE=1 开启",
)

RAND = uuid.uuid4().hex[:6]
IMAGE_CODE = os.environ.get("LIVE_IMAGE_CODE", "sandbox-code:2026.10.22-1")
IMAGE_FS = os.environ.get("LIVE_IMAGE_FS", "sandbox-fs:2026.10.06-1")
GW_PORT = 18555
# 测试里所有命令都受这个上限约束（平台对每个 job 生效），单条命令须远短于它
JOB_MAX = 12

PROBE = r"""
import concurrent.futures, json, socket, sys

def probe(t):
    name, host, port = t
    s = socket.socket(); s.settimeout(2)
    try:
        s.connect((host, port)); return name, "open"
    except socket.timeout:
        return name, "timeout"
    except ConnectionRefusedError:
        return name, "refused"
    except OSError as exc:
        return name, f"error:{exc.errno}"
    finally:
        s.close()

with concurrent.futures.ThreadPoolExecutor(16) as pool:
    print(json.dumps(dict(pool.map(probe, json.loads(sys.argv[1])))))
"""


@pytest.fixture
def live(monkeypatch):
    for key in ("MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY"):
        if not os.environ.get(key):
            pytest.skip(f"缺少 {key}")
    root = REPO_ROOT / "var" / f"it-{RAND}"
    patches = {
        "CONTAINER_LABEL": f"sandboxhub.it{RAND}",
        "SANDBOX_NETWORK": f"sbit{RAND}",
        "SANDBOX_NETWORK_ISOLATED": f"sbit{RAND}-iso",
        "SANDBOX_GATEWAY_NAME": f"sbit{RAND}-gw",
        "SANDBOX_GATEWAY_FORWARDS": f"{GW_PORT}=host.docker.internal:{GW_PORT}",
        "WORK_AREA_ROOT": str(root),
        "SANDBOX_HUB_API_KEY": "live-key",
        "SANDBOX_HUB_HOST": "127.0.0.1",
        "MINIO_ENDPOINT": os.environ["MINIO_ENDPOINT"],
        "MINIO_ACCESS_KEY": os.environ["MINIO_ACCESS_KEY"],
        "MINIO_SECRET_KEY": os.environ["MINIO_SECRET_KEY"],
        "MINIO_SECURE": False,
        "WORK_AREA_ENABLED": True,
        "WORK_AREA_JOB_MAX_LIFETIME": JOB_MAX,
        "WORK_AREA_SELFTEST_INTERVAL": 10**6,
        "ROLE_MOUNT_UNMOUNT_GRACE": 0,
        "DOCKER_IMAGE_CODE_V2": IMAGE_CODE,
        "DOCKER_IMAGE_FS": IMAGE_FS,
        "RCLONE_VFS_WRITE_BACK": "1s",
        "SANDBOX_HTTP_PROXY": "",
    }
    for name, value in patches.items():
        monkeypatch.setattr(settings, name, value)
    return root


def _minio():
    from minio import Minio

    return Minio(
        settings.MINIO_ENDPOINT,
        settings.MINIO_ACCESS_KEY,
        settings.MINIO_SECRET_KEY,
        secure=False,
    )


async def _sh(instance, command: str, timeout: float = 60) -> tuple[int, str]:
    """在实例里跑命令（旧契约：阻塞至结束），返回 (exit_code, output)。"""
    info = instance.container_info
    async with httpx.AsyncClient(timeout=timeout + 15, trust_env=False) as client:
        r = await client.post(
            f"http://{info.container_ip}:8000/api/terminal/execute",
            json={"command": command, "timeout": timeout},
            headers={"X-Sandbox-Token": instance.api_token},
        )
    r.raise_for_status()
    data = r.json()
    return data["exit_code"], data["output"]


def _cleanup_docker(root) -> None:
    label = settings.CONTAINER_LABEL
    script = f"""
set +e
for c in $(docker ps -aq --filter label={label}=true) {settings.wa_gateway_name} {settings.fs_sidecar_name}; do
  docker rm -f "$c" >/dev/null 2>&1
done
for c in $(docker ps -aq --filter label=sandboxhub.probe={label}); do docker rm -f "$c" >/dev/null 2>&1; done
docker network rm {settings.wa_network} {settings.wa_network_isolated} >/dev/null 2>&1
if [ -d "{root}" ]; then
  docker run --rm --privileged -v "{root}:/v:rshared" --entrypoint sh {IMAGE_FS} -c \
    'for m in /v/mnt/*; do umount -l "$m" 2>/dev/null; done; rm -rf /v/* /v/.[!.]* 2>/dev/null; true' >/dev/null 2>&1
  rmdir "{root}" 2>/dev/null
fi
"""
    subprocess.run(["bash", "-c", script], check=False)


async def test_work_area_isolation_end_to_end(live):
    from src.manager.container_manager import ContainerManager
    from src.manager.fs_sidecar import FsOpError, FsSidecar
    from src.manager.wa_instances import AcquireSpec, WorkAreaError, WorkAreaService
    from src.models import InputFileSpec, RoleMountSpec

    root = live
    bucket = f"sbit-{RAND}"
    mc = _minio()
    mc.make_bucket(bucket)
    skill = b"---\nname: hello\n---\nsay hello\n"
    mc.put_object(
        bucket, "roles/r1/skills/hello/SKILL.md", io.BytesIO(skill), len(skill)
    )
    mc.put_object(bucket, "roles/r1/notes/.keep", io.BytesIO(b""), 0)
    doc = b"user document v1\n"
    sha = hashlib.sha256(doc).hexdigest()
    blob_key = f"userspace/u1/blobs/{sha[:2]}/{sha}"
    mc.put_object(bucket, blob_key, io.BytesIO(doc), len(doc))

    manager = ContainerManager()
    service = WorkAreaService(manager, FsSidecar(manager.docker))
    services = [service]
    skills = RoleMountSpec("skills", bucket, "roles/r1/skills/", "ro")
    notes = RoleMountSpec("notes", bucket, "roles/r1/notes/", "rw")

    def spec(wa: str, **kw) -> AcquireSpec:
        return AcquireSpec(
            work_area_id=wa,
            user_id="u1",
            role_id="r1",
            sandbox_type="code",
            mounts=[skills, notes],
            inputs=[InputFileSpec("docs/brief.txt", bucket, blob_key, sha, len(doc))],
            context={"README.md": "# 工作区说明\n"},
            **kw,
        )

    try:
        # ── 1. 地基 + 自检 ────────────────────────────────────────────────
        await service.startup()
        assert service.started
        caps = await service.run_selftest()
        failing = caps["failing"]
        assert caps["ok"] and caps["sandbox_types"]["code"], json.dumps(
            caps["items"], ensure_ascii=False, indent=1
        )
        assert not failing
        assert caps["items"]["gateway_reachable"]["detail"] == "ok"  # 阳性对照真的跑了

        # ── 2. 分配 ────────────────────────────────────────────────────────
        a, reused = await service.acquire(spec("wa-a"))
        assert not reused
        b, _ = await service.acquire(spec("wa-b"))

        code, out = await _sh(
            a,
            "pwd; ls /workspace; cat /workspace/inputs/docs/brief.txt; "
            "cat /workspace/context/README.md; cat /workspace/role/skills/hello/SKILL.md",
        )
        assert code == 0, out
        assert out.splitlines()[0] == "/workspace/work"
        for name in ("context", "deliverables", "inputs", "role", "work"):
            assert name in out
        assert "user document v1" in out and "工作区说明" in out and "say hello" in out

        # ── 3. 无特权：没有 FUSE、不能挂载、没有存储凭据 ─────────────────────
        code, out = await _sh(
            a,
            "test -e /dev/fuse && echo HAS_FUSE; "
            "mount -t tmpfs t /mnt 2>&1 && echo MOUNTED; "
            "env | grep -ciE 'minio|rclone_config|aws_' ; "
            "grep -c CapEff /proc/self/status; cat /proc/self/status | grep CapEff",
        )
        assert "HAS_FUSE" not in out and "MOUNTED" not in out, out
        cap_eff = int(out.strip().splitlines()[-1].split()[-1], 16)
        assert not cap_eff & (1 << 21), "CAP_SYS_ADMIN 不应存在"
        assert out.strip().splitlines()[-3] == "0", out  # 环境里没有存储凭据

        # ── 4. 只读目录写不进；读写角色目录写回 MinIO ────────────────────────
        for path in (
            "/workspace/inputs/docs/brief.txt",
            "/workspace/context/README.md",
            "/workspace/new.txt",
            "/workspace/role/new.txt",
            "/workspace/role/skills/hello/SKILL.md",
            "/workspace/inputs/new.txt",
            "/workspace/role/skills/new.md",
        ):
            code, out = await _sh(a, f"echo pwned > {path}")
            assert code != 0, f"{path} 竟然可写：{out}"
        code, out = await _sh(a, "echo diary > /workspace/role/notes/today.md && sync")
        assert code == 0, out
        for _ in range(30):
            try:
                assert (
                    mc.get_object(bucket, "roles/r1/notes/today.md").read()
                    == b"diary\n"
                )
                break
            except Exception:
                await asyncio.sleep(1)
        else:
            pytest.fail("rw 角色目录的写入没有回写到 MinIO")
        assert mc.get_object(bucket, blob_key).read() == doc  # inputs 原件未被改

        await service.update_inputs(a.sandbox_id, [])
        code, out = await _sh(a, "test ! -e /workspace/inputs/docs/brief.txt")
        assert code == 0, out
        assert mc.get_object(bucket, blob_key).read() == doc
        await service.update_inputs(a.sandbox_id, spec("wa-a").inputs)

        # ── 5. 网络：存储、宿主端口、其他实例都不可达；网关 MinIO 不转发 ───────
        bridge = next(
            x for x in await manager.wa_bridges() if x.network == settings.wa_network
        )
        mhost, _, mport = settings.MINIO_ENDPOINT.rpartition(":")
        targets = [
            ("minio_endpoint", mhost, int(mport)),
            ("minio_via_bridge", bridge.host_ip, int(mport)),
            ("host_ssh", bridge.host_ip, 22),
            ("host_hub", bridge.host_ip, 8088),
            ("peer_api", b.container_info.container_ip, 8000),
            ("gateway_minio", settings.SANDBOX_GATEWAY_NAME, int(mport)),
            ("gateway_forward", settings.SANDBOX_GATEWAY_NAME, GW_PORT),
        ]
        script = base64.b64encode(PROBE.encode()).decode()
        code, out = await _sh(
            a,
            f"echo {script} | base64 -d > /tmp/probe.py && python3 /tmp/probe.py '{json.dumps(targets)}'",
        )
        assert code == 0, out
        result = json.loads(out.strip().splitlines()[-1])
        for name in (
            "minio_endpoint",
            "minio_via_bridge",
            "host_ssh",
            "host_hub",
            "peer_api",
        ):
            assert result[name] in ("timeout",) or result[name].startswith("error"), (
                name,
                result,
            )
        assert result["gateway_minio"] != "open", result
        assert result["gateway_forward"] == "open", result

        # ── 6. 容器 API 校验每实例 token ──────────────────────────────────
        base = f"http://{a.container_info.container_ip}:8000"
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            assert (
                await client.get(f"{base}/api/terminal/activity")
            ).status_code == 401
            r = await client.get(
                f"{base}/api/terminal/activity",
                headers={"X-Sandbox-Token": b.api_token},
            )
            assert r.status_code == 401  # 别的实例的 token 也不行
            r = await client.get(
                f"{base}/api/terminal/activity",
                headers={"X-Sandbox-Token": a.api_token},
            )
            assert r.status_code == 200
            assert (await client.get(f"{base}/api/system/health")).status_code == 200

        # ── 7. 归档读取：普通文件可读且稳定，软链接与越界路径拒绝 ─────────────
        code, out = await _sh(
            a,
            "echo report > /workspace/deliverables/report.md; "
            "ln -s /workspace/inputs/docs/brief.txt /workspace/deliverables/link.md; "
            "ln -s ../../wa-b/work /workspace/work/escape; echo secret-b > /dev/null",
        )
        assert code == 0, out
        await _sh(b, "echo secret-b > /workspace/work/secret.txt")
        got = await service.work_areas.read("wa-a", "deliverables", "report.md")
        try:
            assert got.path.read_bytes() == b"report\n" and got.stable
            assert got.sha256 == hashlib.sha256(b"report\n").hexdigest()
        finally:
            got.path.unlink(missing_ok=True)
        with pytest.raises(FsOpError) as ei:
            await service.work_areas.read("wa-a", "deliverables", "link.md")
        assert ei.value.not_regular
        with pytest.raises(FsOpError) as ei:
            await service.work_areas.read("wa-a", "work", "escape/secret.txt")
        assert ei.value.not_regular
        listing = await service.work_areas.list_files("wa-a", "deliverables")
        assert [f["path"] for f in listing["files"]] == ["report.md"]
        digests = await service.work_areas.checksums("wa-a", "deliverables", ["report.md", "missing.md"])
        assert digests["schema_version"] == 1
        assert digests["items"][0]["sha256"] == hashlib.sha256(b"report\n").hexdigest()
        assert digests["items"][0]["stable"] is True
        assert digests["items"][1] == {"path": "missing.md", "found": False}
        assert not list((root / "staging").glob("checksums-*.json"))

        # ── 8. 后台 job 超过最长存活时间被终止 ─────────────────────────────
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            headers = {"X-Sandbox-Token": a.api_token}
            r = await client.post(
                f"{base}/api/terminal/execute",
                json={"command": "sleep 300", "wait": 0.5},
                headers=headers,
            )
            job = r.json()
            assert job["status"] == "running"
            for _ in range(JOB_MAX + 20):
                r = await client.post(
                    f"{base}/api/terminal/wait",
                    json={"job_id": job["job_id"], "wait": 1},
                    headers=headers,
                )
                job = r.json()
                if job["status"] != "running":
                    break
            assert job["status"] == "killed" and job["kill_reason"] == "max_lifetime", (
                job
            )

        # ── 9. 释放后工作区保留，下次分配同一工作区能看到 ─────────────────────
        await _sh(a, "echo keep > /workspace/work/keep.txt")
        await service.quiesce(a.sandbox_id)
        stopped = await service.work_areas.read("wa-a", "deliverables", "report.md")
        try:
            assert stopped.path.read_bytes() == b"report\n" and stopped.stable
        finally:
            stopped.path.unlink(missing_ok=True)
        await service.release(a.sandbox_id)
        a2, reused = await service.acquire(spec("wa-a"))
        assert not reused and a2.sandbox_id != a.sandbox_id
        code, out = await _sh(a2, "cat /workspace/work/keep.txt")
        assert out.strip() == "keep"

        # ── 10. 同工作区复用 + 上限 ──────────────────────────────────────
        again, reused = await service.acquire(
            replace(spec("wa-a"), session_id="s-later", turn_id="t-later")
        )
        assert reused and again.sandbox_id == a2.sandbox_id
        with pytest.raises(WorkAreaError) as ei:
            await service.acquire(spec("wa-c"))  # 同一用户的同一数字人已有 2 个实例
        assert ei.value.status == 409 and ei.value.reason == "role_limit"

        # ── 11. Hub 重启：新服务对象从标签收养在跑的实例 ─────────────────────
        manager2 = ContainerManager()
        service2 = WorkAreaService(manager2, FsSidecar(manager2.docker))
        services.append(service2)
        await service2.startup()
        adopted = {r.sandbox_id for r in service2.registry.list_all()}
        assert adopted == {a2.sandbox_id, b.sandbox_id}
        assert service2.mounts.refs() == {skills.key: 2, notes.key: 2}
        assert service2.registry.get(a2.sandbox_id).api_token == a2.api_token
        assert service2.registry.get(a2.sandbox_id).session_id == "s-later"
        assert service2.registry.get(a2.sandbox_id).turn_id == "t-later"
    finally:
        for svc in reversed(services):
            for instance in svc.registry.list_all():
                try:
                    await svc._discard(instance, "test_cleanup")
                except Exception:
                    pass
            try:
                await svc.mounts.sweep(0)
            except Exception:
                pass
        try:
            await service.netguard.teardown()
        except Exception:
            pass
        _cleanup_docker(root)
        try:
            for obj in mc.list_objects(bucket, recursive=True):
                mc.remove_object(bucket, obj.object_name)
            mc.remove_bucket(bucket)
        except Exception:
            pass
