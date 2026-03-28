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
