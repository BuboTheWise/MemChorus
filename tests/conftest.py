"""Pytest configuration for MemChorus test suite."""
import asyncio
import gc
import inspect as _inspect
import sys
import types as _types
import pytest


@pytest.fixture(autouse=True, scope="module")
def _cleanup_asyncio_between_modules():
    """Cleanup asyncio state when switching between test modules."""
    _asyncio_cleanup()
    yield
    _asyncio_cleanup()
    gc.collect()


@pytest.fixture(autouse=True)
def _teardown_sweep_coroutines(request):
    """Install coro-lifecycle suppressor and teardown after each test."""
    hook = _install_unraisable_suppressor(request.node.name)
    yield
    _restore_unraisable_hook(hook)
    gc.collect()
    _close_all_coros()


_KNOWN_LEAK_PATTERNS = [
    "_call_tool_async",
    "connect.<locals>._do_init",
    "wait_for",
    "broken",
]

# ── Unraisable Exception Hook Suppression ──────────────────────────────

_original_unraisable_hook = None


def _install_unraisable_suppressor(test_id=None):
    """Replace sys.unraisable hook so coro-lifecycle warnings from known leaks are dropped."""
    global _original_unraisable_hook
    _original_unraisable_hook = sys.unraisablehook

    def _suppressed_hook(exc, value, tb):
        if not isinstance(value, RuntimeError):
            _original_unraisable_hook(exc, value, tb)
            return
        msg = str(value).lower()
        if "coroutine" not in msg:
            _original_unraisable_hook(exc, value, tb)
            return
        # Suppress only the coroutines we know are mock artifacts
        for needle in _KNOWN_LEAK_PATTERNS:
            if needle.lower() in msg:
                return  # drop silently
        # Let unraisables through
        _original_unraisable_hook(exc, value, tb)

    sys.unraisablehook = _suppressed_hook


def _restore_unraisable_hook(_):
    """Restore the original unraisable hook."""
    global _original_unraisable_hook
    if _original_unraisable_hook is not None:
        sys.unraisablehook = _original_unraisable_hook
        _original_unraisable_hook = None


def safe_side_effect(exc_factory):
    """Side effect wrapper that aggressively closes ALL leaked coroutines before raising.

    When tests mock _run_async, the coroutine arg (wait_for(inner_coro)) is passed
    but never awaited because we raise in the side_effect handler. That leaves both
    the outer wait_for AND inner targets (_call_tool_async or _do_init) unreachable
    when their last reference disappears - Python's GC will finalizer them and emit
    RuntimeWarning.

    This wrapper closes args[0] AND runs gc.collect() + sweeps all coroutines by name
    BEFORE raising, ensuring everything is .close()d while references still exist.

    Usage: side_effect=safe_side_effect(lambda *a, **k: ExceptionGroup(...))
    """
    def wrapper(*args, **kwargs):
        # Close the primary coroutine argument (wait_for wrapper)
        if args:
            first = args[0]
            if _inspect.iscoroutine(first) or isinstance(first, _types.CoroutineType):
                try:
                    first.close()
                except RuntimeError:
                    pass  # already closed or awaited

        gc.collect()
        _close_all_coros_by_name(_KNOWN_LEAK_PATTERNS)
        raise exc_factory(*args, **kwargs)
    return wrapper


def safe_return_value(value):
    """Side effect that closes coro args before returning *value* instead of raising.

    When mocking _run_async or asyncio.run with a non-exception (e.g., None to simulate
    graceful failure), the coroutine argument still exists and needs draining - otherwise
    it becomes unreachable after mock teardown and emits RuntimeWarning on GC finalization.

    Usage: side_effect=safe_return_value(None)  # instead of return_value=None
    """
    def wrapper(*args, **kwargs):
        if args:
            first = args[0]
            if _inspect.iscoroutine(first) or isinstance(first, _types.CoroutineType):
                try:
                    first.close()
                except RuntimeError:
                    pass  # already closed or awaited

        gc.collect()
        _close_all_coros_by_name(_KNOWN_LEAK_PATTERNS)
        return value
    return wrapper


def _close_all_coros_by_name(names):
    """Find and close coroutine objects in GC whose __qualname__ matches *names*."""
    seen = set()
    for obj in gc.get_objects():
        if not isinstance(obj, _types.CoroutineType):
            continue
        qn = getattr(obj, '__qualname__', '')
        fn = getattr(getattr(obj, 'cr_code', None), '__qualname__', '') if hasattr(obj, 'cr_code') else ''
        matched = any(needle in qn or needle in fn for needle in names)
        if matched and obj not in seen:
            try:
                obj.close()
                seen.add(obj)
            except RuntimeError:
                pass  # already closed or awaited


def _close_all_coros():
    """Close ALL coroutine objects currently tracked by GC."""
    seen = set()
    for obj in gc.get_objects():
        if isinstance(obj, _types.CoroutineType) and obj not in seen:
            try:
                obj.close()
                seen.add(obj)
            except RuntimeError:
                pass


def _asyncio_cleanup():
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return
    if loop.is_closed():
        return
    tasks = asyncio.all_tasks(loop) if hasattr(asyncio, 'all_tasks') else []
    cancellable = [t for t in tasks if not t.done()]
    for task in cancellable:
        task.cancel()
    if cancellable:
        gather_fut = asyncio.gather(*cancellable, return_exceptions=True)
        try:
            loop.run_until_complete(gather_fut)
        except RuntimeError:
            pass
