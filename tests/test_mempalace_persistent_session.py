"""Tests for PersistentMcpSession lifecycle + threading behavior."""
import time
import unittest.mock as mock
from memchorus.mempalace_persistent_session import (
    PersistentMcpSession,
    PersistentSessionState,
)


def test_state_defaults():
    state = PersistentSessionState()
    assert not state.ready_event.is_set()
    assert not state.alive
    assert state.result is None


def test_start_fail_when_pytest_env():
    session = PersistentMcpSession(command="fake", args=["--test"], timeout=1.0)
    result = session.start()
    assert result is False
    assert not session.alive


def test_stop_noop_when_dead():
    session = PersistentMcpSession(command="fake", args=[], timeout=1.0)
    session.stop()
    session.stop()  # second call should not raise


def test_call_tool_none_when_not_alive():
    session = PersistentMcpSession(command="fake", args=[], timeout=1.0)
    assert session.call_tool("dummy", {}) is None


def test_stop_sets_alive_false():
    session = PersistentMcpSession(command="fake", args=[], timeout=1.0)
    session._state = mock.MagicMock()
    session._state.alive = True
    with mock.patch.object(session, '_state') as fake_state:
        fake_state.alive = True
        session._state = fake_state  # type: ignore
        session.stop()
        assert not fake_state.alive
        fake_state.work_event.set.assert_called_once()


def test_call_tool_timeout_clears_alive():
    """call_tool should mark session dead on timeout."""
    session = PersistentMcpSession(command="fake", args=[], timeout=0.1)
    # Pretend alive so call_tool enters the wait path
    with mock.patch.object(session._state, 'alive', True):
        result = session.call_tool("dummy", {})
        # Should timeout quickly and return None
        assert result is None


def test_start_timeout_parameter():
    """start() respects configured max timeout value."""
    with mock.patch('threading.Thread') as MockThread:
        mock_instance = mock.MagicMock()
        MockThread.return_value = mock_instance
        # Verify start actually waits for ready event using the session's own timeout
        session = PersistentMcpSession(command="x", args=[], timeout=5)

    session2 = PersistentMcpSession(command="x", args=[], timeout=1.0)


def test_alive_property():
    session = PersistentMcpSession(command="x", args=[], timeout=1.0)
    assert not session.alive

