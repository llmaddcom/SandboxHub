import pytest
from unittest.mock import AsyncMock, MagicMock
from src.manager.warm_pool import WarmPool
from src.models import ContainerInfo


def make_container(ip: str = "172.17.0.5") -> ContainerInfo:
    return ContainerInfo(
        container_id=f"cid_{ip}",
        container_name="cr-sb-warm-ubuntu-1-abc",
        container_ip=ip,
        sandbox_type="ubuntu",
    )


@pytest.fixture
def mock_manager():
    m = MagicMock()
    m.run_container = AsyncMock(return_value=make_container())
    m.remove_container = AsyncMock()
    m.clean_and_reset = AsyncMock()
    return m


@pytest.mark.asyncio
async def test_acquire_returns_container_when_pool_has_items(mock_manager):
    pool = WarmPool(mock_manager)
    container = make_container()
    pool._pools["ubuntu"].append(container)
    result = await pool.acquire("ubuntu")
    assert result is container
    assert pool.available_count("ubuntu") == 0


@pytest.mark.asyncio
async def test_acquire_returns_none_when_pool_empty(mock_manager):
    pool = WarmPool(mock_manager)
    result = await pool.acquire("ubuntu")
    assert result is None


@pytest.mark.asyncio
async def test_available_count(mock_manager):
    pool = WarmPool(mock_manager)
    pool._pools["ubuntu"].append(make_container("172.17.0.5"))
    pool._pools["ubuntu"].append(make_container("172.17.0.6"))
    assert pool.available_count("ubuntu") == 2


@pytest.mark.asyncio
async def test_release_cleans_and_returns_to_pool(mock_manager):
    pool = WarmPool(mock_manager)
    container = make_container()
    await pool.release(container)
    mock_manager.clean_and_reset.assert_awaited_once_with(container.container_ip)
    assert pool.available_count("ubuntu") == 1


@pytest.mark.asyncio
async def test_refill_adds_containers_to_pool(mock_manager):
    pool = WarmPool(mock_manager)
    # pool is empty, target size is 2 for this test
    await pool._refill("ubuntu", target=2)
    assert mock_manager.run_container.await_count == 2
    assert pool.available_count("ubuntu") == 2


@pytest.mark.asyncio
async def test_refill_skips_when_pool_already_full(mock_manager):
    pool = WarmPool(mock_manager)
    pool._pools["ubuntu"].append(make_container("172.17.0.5"))
    pool._pools["ubuntu"].append(make_container("172.17.0.6"))
    await pool._refill("ubuntu", target=2)
    mock_manager.run_container.assert_not_called()
