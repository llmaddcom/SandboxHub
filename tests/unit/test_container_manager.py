import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.manager.container_manager import ContainerManager
from src.config import settings


def make_mock_container(ip: str = "172.17.0.5", status: str = "running"):
    c = MagicMock()
    c.id = "container_abc"
    c.name = "cr-sb-warm-1"
    c.status = status
    c.attrs = {
        "NetworkSettings": {
            "Networks": {
                "bridge": {"IPAddress": ip}
            }
        }
    }
    c.reload = MagicMock()
    return c


@pytest.fixture
def mock_docker():
    with patch("src.manager.container_manager.docker") as mock:
        client = MagicMock()
        mock.from_env.return_value = client
        yield client


def test_get_container_ip_bridge(mock_docker):
    manager = ContainerManager()
    container = make_mock_container(ip="172.17.0.5")
    ip = manager._get_container_ip(container)
    assert ip == "172.17.0.5"


def test_get_container_ip_raises_when_no_ip(mock_docker):
    manager = ContainerManager()
    container = MagicMock()
    container.name = "test"
    container.attrs = {"NetworkSettings": {"Networks": {}}}
    with pytest.raises(RuntimeError, match="无法获取容器 IP"):
        manager._get_container_ip(container)


@pytest.mark.asyncio
async def test_is_healthy_returns_true_on_connection(mock_docker):
    manager = ContainerManager()
    with patch("asyncio.open_connection", new_callable=AsyncMock) as mock_conn:
        writer = MagicMock()
        writer.wait_closed = AsyncMock()
        mock_conn.return_value = (MagicMock(), writer)
        result = await manager.is_healthy("172.17.0.5")
    assert result is True


@pytest.mark.asyncio
async def test_is_healthy_returns_false_on_error(mock_docker):
    manager = ContainerManager()
    with patch("asyncio.open_connection", side_effect=ConnectionRefusedError):
        result = await manager.is_healthy("172.17.0.5")
    assert result is False


def test_build_container_name(mock_docker):
    manager = ContainerManager()
    name = manager._build_warm_name("ubuntu", 3)
    assert name.startswith("cr-sb-warm-")
    assert "ubuntu" in name


# ── SANDBOX_KEEP_DNS 注入（entrypoint 据此跳过 resolv.conf 覆写）─────────────────


def test_env_no_keep_dns_by_default(mock_docker, monkeypatch):
    """默认（无 SANDBOX_DNS、开关关闭）不注入，行为与现状一致。"""
    monkeypatch.setattr(settings, "SANDBOX_DNS", "")
    monkeypatch.setattr(settings, "SANDBOX_KEEP_DNS", False)
    env = ContainerManager()._build_container_env("ubuntu")
    assert "SANDBOX_KEEP_DNS" not in env


def test_env_keep_dns_when_sandbox_dns_configured(mock_docker, monkeypatch):
    """配置了自定义 DNS（docker --dns）时自动注入，使 --dns 真正生效。"""
    monkeypatch.setattr(settings, "SANDBOX_DNS", "10.0.0.2,10.0.0.3")
    monkeypatch.setattr(settings, "SANDBOX_KEEP_DNS", False)
    for sandbox_type in ("ubuntu", "code"):
        env = ContainerManager()._build_container_env(sandbox_type)
        assert env["SANDBOX_KEEP_DNS"] == "1"


def test_env_keep_dns_when_forced_by_flag(mock_docker, monkeypatch):
    """SANDBOX_KEEP_DNS=true 时即使未配 SANDBOX_DNS 也强制注入。"""
    monkeypatch.setattr(settings, "SANDBOX_DNS", "")
    monkeypatch.setattr(settings, "SANDBOX_KEEP_DNS", True)
    env = ContainerManager()._build_container_env("ubuntu")
    assert env["SANDBOX_KEEP_DNS"] == "1"


# ── v1 归还复位：上一任租户的长驻进程（SandboxHub#52）一并回收 ───────────────────


def _reset_calls(monkeypatch, procs_status: int, procs: list[dict]) -> list[tuple[str, str]]:
    import httpx

    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/api/procs":
            return httpx.Response(procs_status, json={"procs": procs} if procs_status == 200 else {"detail": "Not Found"})
        return httpx.Response(200, json={"ok": True})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        "src.manager.container_manager.httpx.AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs),
    )
    return calls


@pytest.mark.asyncio
async def test_clean_and_reset_removes_leftover_procs_first(mock_docker, monkeypatch):
    calls = _reset_calls(monkeypatch, 200, [{"proc_id": "p_1"}, {"proc_id": "p_2"}])
    await ContainerManager().clean_and_reset("172.17.0.5")
    assert calls[:3] == [("GET", "/api/procs"), ("DELETE", "/api/procs/p_1"), ("DELETE", "/api/procs/p_2")]
    assert ("POST", "/api/terminal/restart") in calls


@pytest.mark.asyncio
async def test_clean_and_reset_tolerates_images_without_procs(mock_docker, monkeypatch):
    calls = _reset_calls(monkeypatch, 404, [])
    await ContainerManager().clean_and_reset("172.17.0.5")
    assert not any(method == "DELETE" for method, _ in calls)


@pytest.mark.asyncio
async def test_clean_and_reset_fails_when_procs_cannot_be_listed(mock_docker, monkeypatch):
    _reset_calls(monkeypatch, 500, [])
    with pytest.raises(RuntimeError, match="clean_and_reset"):
        await ContainerManager().clean_and_reset("172.17.0.5")   # 调用方据此销毁容器，不回池
