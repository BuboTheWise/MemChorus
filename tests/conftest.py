"""Pytest configuration for MemChorus test suite."""
import asyncio
import gc
import pytest


@pytest.fixture(autouse=True, scope="module")
def _cleanup_asyncio_between_modules():
    """Fire before and after each test module to clear leaked coroutines."""
    _asyncio_cleanup()
    gc.collect()
    yield
    _asyncio_cleanup()
    gc.collect()


def _asyncio_cleanup():
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return
    tasks = asyncio.all_tasks(loop) if hasattr(asyncio, 'all_tasks') else []
    for task in tasks:
        if not task.done():
            task.cancel()
