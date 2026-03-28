import pytest
from datetime import datetime
from src.manager.registry import SandboxRegistry
from src.models import ContainerInfo, SandboxRecord


def make_container(ip: str = "172.17.0.5") -> ContainerInfo:
    return ContainerInfo(
        container_id="abc123",
        container_name="cr-sb-warm-1",
        container_ip=ip,
        sandbox_type="ubuntu",
    )


@pytest.mark.asyncio
async def test_register_and_get():
    reg = SandboxRegistry()
    record = await reg.register(make_container(), user_id="u1", role_id="r1")
    assert record.sandbox_id
    assert record.status == "ready"
    fetched = await reg.get(record.sandbox_id)
    assert fetched is record


@pytest.mark.asyncio
async def test_get_unknown_returns_none():
    reg = SandboxRegistry()
    assert await reg.get("nonexistent") is None


@pytest.mark.asyncio
async def test_find_active_returns_existing():
    reg = SandboxRegistry()
    record = await reg.register(make_container(), user_id="u1", role_id="r1")
    found = await reg.find_active("u1", "r1")
    assert found is record


@pytest.mark.asyncio
async def test_find_active_returns_none_for_unknown():
    reg = SandboxRegistry()
    assert await reg.find_active("u99", "r99") is None


@pytest.mark.asyncio
async def test_find_active_returns_none_after_release():
    reg = SandboxRegistry()
    record = await reg.register(make_container(), user_id="u1", role_id="r1")
    await reg.mark_released(record.sandbox_id)
    assert await reg.find_active("u1", "r1") is None


@pytest.mark.asyncio
async def test_mark_released_returns_container_info():
    reg = SandboxRegistry()
    container = make_container()
    record = await reg.register(container, user_id="u1", role_id="r1")
    returned = await reg.mark_released(record.sandbox_id)
    assert returned is container


@pytest.mark.asyncio
async def test_mark_released_unknown_returns_none():
    reg = SandboxRegistry()
    assert await reg.mark_released("nonexistent") is None


@pytest.mark.asyncio
async def test_list_all():
    reg = SandboxRegistry()
    await reg.register(make_container("172.17.0.5"), user_id="u1", role_id="r1")
    await reg.register(make_container("172.17.0.6"), user_id="u2", role_id="r2")
    assert len(reg.list_all()) == 2
