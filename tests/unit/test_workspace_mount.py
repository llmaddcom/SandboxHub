"""工作区挂载（云盘 MinIO ↔ 容器）单元测试。

覆盖：config.mount_ready / rclone env、container_manager 的 FUSE 能力注入与 rclone 挂载命令、
run_container 挂载成败路径、warm_pool 对挂载容器的销毁（不入池、不 rm -rf）。
全部 mock Docker，不连真实容器/MinIO。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.config import Settings, settings
from src.manager.container_manager import ContainerManager
from src.manager.warm_pool import WarmPool
from src.models import ContainerInfo, WorkspaceMount


def _ws(mount_path: str = "/workspace") -> WorkspaceMount:
    return WorkspaceMount(bucket="cr-ws", prefix="roles/r1", mount_path=mount_path)


# ── config ────────────────────────────────────────────────────────────────────

def test_mount_ready_false_without_creds():
    s = Settings(WORKSPACE_MOUNT_ENABLED=True, MINIO_ENDPOINT="", MINIO_ACCESS_KEY="", MINIO_SECRET_KEY="")
    assert s.mount_ready is False


def test_mount_ready_false_when_disabled():
    s = Settings(WORKSPACE_MOUNT_ENABLED=False, MINIO_ENDPOINT="m:9000", MINIO_ACCESS_KEY="a", MINIO_SECRET_KEY="b")
    assert s.mount_ready is False


def test_mount_ready_true_with_creds():
    s = Settings(WORKSPACE_MOUNT_ENABLED=True, MINIO_ENDPOINT="m:9000", MINIO_ACCESS_KEY="a", MINIO_SECRET_KEY="b")
    assert s.mount_ready is True


def test_minio_rclone_env_shape():
    s = Settings(MINIO_ENDPOINT="minio:9000", MINIO_ACCESS_KEY="ak", MINIO_SECRET_KEY="sk", MINIO_SECURE=False)
    env = s.minio_rclone_env()
    assert env["RCLONE_CONFIG_MINIO_TYPE"] == "s3"
    assert env["RCLONE_CONFIG_MINIO_PROVIDER"] == "Minio"
    assert env["RCLONE_CONFIG_MINIO_ACCESS_KEY_ID"] == "ak"
    assert env["RCLONE_CONFIG_MINIO_SECRET_ACCESS_KEY"] == "sk"
    assert env["RCLONE_CONFIG_MINIO_ENDPOINT"] == "http://minio:9000"


def test_minio_rclone_env_https_when_secure():
    s = Settings(MINIO_ENDPOINT="minio:9000", MINIO_ACCESS_KEY="ak", MINIO_SECRET_KEY="sk", MINIO_SECURE=True)
    assert s.minio_rclone_env()["RCLONE_CONFIG_MINIO_ENDPOINT"] == "https://minio:9000"


# ── container_manager: docker mock ──────────────────────────────────────────────

@pytest.fixture
def mock_docker():
    with patch("src.manager.container_manager.docker") as mock:
        client = MagicMock()
        mock.from_env.return_value = client
        yield client


def _running_container(ip: str = "172.17.0.9"):
    c = MagicMock()
    c.id = "cid_xyz"
    c.status = "running"
    c.attrs = {"NetworkSettings": {"Networks": {"bridge": {"IPAddress": ip}}}}
    c.reload = MagicMock()
    return c


def test_run_container_sync_with_workspace_adds_fuse(mock_docker):
    mock_docker.containers.run.return_value = _running_container()
    mgr = ContainerManager()
    mgr._run_container_sync("code", "cr-sb-mnt-x", _ws())
    kwargs = mock_docker.containers.run.call_args.kwargs
    assert kwargs["cap_add"] == ["SYS_ADMIN"]
    assert kwargs["devices"] == ["/dev/fuse:/dev/fuse:rwm"]
    assert "apparmor:unconfined" in kwargs["security_opt"]
    assert kwargs["labels"].get("sandboxhub.mounted") == "true"


def test_run_container_sync_without_workspace_no_fuse(mock_docker):
    mock_docker.containers.run.return_value = _running_container()
    mgr = ContainerManager()
    mgr._run_container_sync("code", "cr-sb-warm-x", None)
    kwargs = mock_docker.containers.run.call_args.kwargs
    assert "cap_add" not in kwargs
    assert "devices" not in kwargs
    assert "sandboxhub.mounted" not in kwargs["labels"]


def test_mount_workspace_sync_builds_rclone_command(mock_docker, monkeypatch):
    monkeypatch.setattr(settings, "MINIO_ENDPOINT", "minio:9000")
    monkeypatch.setattr(settings, "MINIO_ACCESS_KEY", "ak")
    monkeypatch.setattr(settings, "MINIO_SECRET_KEY", "sk")
    monkeypatch.setattr(settings, "RCLONE_VFS_CACHE_MODE", "full")
    container = MagicMock()
    mock_docker.containers.get.return_value = container
    mgr = ContainerManager()
    mgr._mount_workspace_sync("cid_xyz", _ws(mount_path="/workspace"))

    call = container.exec_run.call_args
    cmd = call.kwargs["cmd"][-1]
    assert "rclone mount minio:cr-ws/roles/r1 /workspace" in cmd
    # full 模式：写回窗口内 rename-over（sed -i）在 writes 模式下报 EIO（issue #9）
    assert "--vfs-cache-mode full" in cmd
    assert "--vfs-cache-max-size" in cmd
    assert call.kwargs["detach"] is True
    assert call.kwargs["environment"]["RCLONE_CONFIG_MINIO_ACCESS_KEY_ID"] == "ak"


@pytest.mark.asyncio
async def test_run_container_mounts_and_returns_mounted(mock_docker):
    mgr = ContainerManager()
    mgr._run_container_sync = MagicMock(return_value=("cid_xyz", "172.17.0.9"))
    mgr.wait_healthy = AsyncMock(return_value=True)
    mgr.mount_workspace = AsyncMock()

    info = await mgr.run_container("code", workspace=_ws())

    assert isinstance(info, ContainerInfo)
    assert info.mounted is True
    assert info.mount_path == "/workspace"
    mgr.mount_workspace.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_container_mount_failure_destroys_and_raises(mock_docker):
    mgr = ContainerManager()
    mgr._run_container_sync = MagicMock(return_value=("cid_xyz", "172.17.0.9"))
    mgr.wait_healthy = AsyncMock(return_value=True)
    mgr.mount_workspace = AsyncMock(side_effect=RuntimeError("mount boom"))
    mgr._stop_and_remove_sync = MagicMock()

    with pytest.raises(RuntimeError, match="工作区挂载失败"):
        await mgr.run_container("code", workspace=_ws())
    mgr._stop_and_remove_sync.assert_called_once_with("cid_xyz")


@pytest.mark.asyncio
async def test_run_container_without_workspace_is_unmounted(mock_docker):
    mgr = ContainerManager()
    mgr._run_container_sync = MagicMock(return_value=("cid_warm", "172.17.0.5"))
    mgr.wait_healthy = AsyncMock(return_value=True)
    mgr.mount_workspace = AsyncMock()

    info = await mgr.run_container("ubuntu")
    assert info.mounted is False
    mgr.mount_workspace.assert_not_called()


# ── 代理注入：仅桌面镜像，code 镜像直连 ─────────────────────────────────────────

def test_code_container_env_omits_proxy(mock_docker, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_HTTP_PROXY", "http://host.docker.internal:8118")
    mgr = ContainerManager()
    env = mgr._build_container_env("code")
    assert "HTTP_PROXY" not in env and "http_proxy" not in env


def test_ubuntu_container_env_keeps_proxy(mock_docker, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_HTTP_PROXY", "http://host.docker.internal:8118")
    mgr = ContainerManager()
    env = mgr._build_container_env("ubuntu")
    assert env["HTTP_PROXY"] == "http://host.docker.internal:8118"


# ── warm_pool: 挂载容器释放即销毁 ───────────────────────────────────────────────

def _mounted_container() -> ContainerInfo:
    return ContainerInfo(
        container_id="cid_mnt",
        container_name="cr-sb-mnt-code-abc",
        container_ip="172.17.0.9",
        sandbox_type="code",
        mounted=True,
        mount_path="/workspace",
    )


@pytest.mark.asyncio
async def test_release_mounted_unmounts_and_destroys_not_pool():
    mgr = MagicMock()
    mgr.unmount_workspace = AsyncMock()
    mgr.remove_container = AsyncMock()
    mgr.clean_and_reset = AsyncMock()
    pool = WarmPool(mgr)

    with patch("src.proxy.forwarder.close_client", new_callable=AsyncMock):
        await pool.release(_mounted_container())

    mgr.unmount_workspace.assert_awaited_once_with("cid_mnt", "/workspace")
    mgr.remove_container.assert_awaited_once_with("cid_mnt")
    mgr.clean_and_reset.assert_not_called()  # 绝不对挂载点 rm -rf
    assert pool.available_count("code") == 0  # 不入池
