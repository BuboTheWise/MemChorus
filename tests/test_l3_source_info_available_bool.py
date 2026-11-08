"""
test_l3_source_info_available_bool.py — GH-270 L3 regression test.

L3 is the *regression* arm of the batch: no source change, one new test file
that locks an invariant the whole recall path depends on.

Invariant:
  MemorySource.get_source_info() → {"...": ..., "available": <bool>, ...}

History (root of the regression):
  - GAP014 was the original `'bool' object is not callable` crash when a
    subclass defined `is_available` as a method (`def is_available(self)`)
    while `get_source_info` returned `'available': self.is_available`.
    A bound method is truthy-but-not-a-bool, so downstream truthiness checks
    "worked" by accident, but any code doing a strict type check, JSON
    serialisation, or a boolean comparison (`is True` / `is False`)
    silently got the wrong value.
  - The ABC (memory_source.py) was fixed to define `is_available` as a
    `@property` (read-only boolean descriptor) so *subclasses must implement
    it as a property*. Every concrete source (hermes_default, mempalace,
    session_history) follows that contract today.

This test pins:
  1. Real source implementations expose ``available`` as a ``bool`` —
     not as a bound method, not as a string, not as a truthy value.
  2. The ABC contract itself says ``is_available`` is a property, so a
     future "cleanup" PR that reverts the ABC to a plain abstract method
     will fail this test (GAP014 regression).
  3. A synthetic subclass that *violates* the contract (defines it as a
     method) still produces a truthy-but-not-a-bool ``available`` value —
     recorded as documentation of the failure mode the test guards against.

No source code change is required for L3 to pass: the invariant is
already true. This file exists solely as a regression barrier.
"""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.hermes_memory_source import HermesDefaultMemorySource
from memchorus.memory_source import MemorySource
from memchorus.session_search_memory_source import (
    SessionSearchMemorySource,
)


@pytest.fixture
def hermes_source(tmp_path):
    """Real hermes_default source pinned to a temp dir (GAP046 positional pattern)."""
    d = tmp_path / "mem"
    d.mkdir(parents=True, exist_ok=True)
    return HermesDefaultMemorySource(str(d))


@pytest.fixture
def session_source(tmp_path, monkeypatch):
    """Real session_history source pointed at an in-memory-ish temp DB.

    Point the ``session_db_path`` config at a non-existent path so
    ``_get_connection`` returns ``None`` (source not available), giving us a
    deterministic False without depending on the live state.db.
    """
    db = tmp_path / "state.db"  # non-existent; the source treats that as unavailable
    return SessionSearchMemorySource(name="session_history", config={"session_db_path": str(db)})


def _is_bool_descriptor(cls, attr):
    """Return True iff ``attr`` on ``cls`` (or any base) is a ``property`` descriptor."""
    for klass in cls.__mro__:
        if attr in vars(klass):
            return isinstance(vars(klass)[attr], property)
    return False


# --- 1. Real sources expose available as a bool ----------------------------


def test_hermes_source_available_is_bool(hermes_source):
    """HermesDefaultMemorySource.get_source_info()['available'] must be a bool."""
    info = hermes_source.get_source_info()
    assert "available" in info
    v = info["available"]
    assert isinstance(v, bool), (
        f"available must be a bool, got {type(v).__name__}: {v!r} (GAP014 regression)"
    )
    # And the value must match the property directly (no truthiness drift).
    assert v is hermes_source.is_available


def test_session_source_available_is_bool(session_source):
    """SessionSearchMemorySource.get_source_info()['available'] must be a bool."""
    info = session_source.get_source_info()
    assert "available" in info
    v = info["available"]
    assert isinstance(v, bool), (
        f"available must be a bool, got {type(v).__name__}: {v!r} (GAP014 regression)"
    )
    assert v is session_source.is_available


# --- 2. The ABC pins is_available as a property (GAP014 barrier) -----------


def test_abc_pins_is_available_as_property():
    """GAP014 guard: MemorySource.is_available must be a read-only property.

    If a future PR re-introduces it as an abstract *method* (plain
    ``def``) so that subclasses can define it as a method too, the whole
    ``available: bool`` contract in every ``get_source_info()`` collapses
    into "truthy but not a bool". This test fails that change before merge.
    """
    assert _is_bool_descriptor(MemorySource, "is_available"), (
        "MemorySource.is_available must be defined as a @property "
        "(GAP014). If it reverted to a plain abstractdef, subclasses can "
        "define it as a method and get_source_info()['available'] will "
        "regress to a bound callable — truthy-but-not-a-bool."
    )
    # And the descriptor must be read-only (no setter) so subclasses can't
    # shadow it with an assignment that flips to a mutable value.
    prop = vars(MemorySource)["is_available"]
    assert prop.fset is None, (
        "MemorySource.is_available should be a read-only @property "
        "(no setter). A mutable slot invites the GAP014 regression."
    )


# --- 3. Documented failure mode: what GAP014 actually produces ------------


class _SyntheticMethodStyle(MemorySource):
    """A MemorySource that violates the contract (is_available as a method).

    This is exactly the shape GAP014 came from. We instantiate it, call
    get_source_info, and lock the *observable* consequence: ``available``
    becomes a bound method — truthy but not a bool.
    """

    def __init__(self):
        super().__init__("synthetic_method_style", None)
        self._name = "synthetic_method_style"
        self.config = {}

    def is_available(self):  # pyright: ignore[reportIncompatibleMethodOverride]
        # GAP014 shape: a *method* instead of a property. This is the deliberate
        # regression the test documents — the ABC expects a property.
        return True

    def save(self, key, value):  # pragma: no cover
        return True

    def retrieve(self, key):  # pragma: no cover
        return None

    def search(self, query, limit=10):  # pragma: no cover
        return []

    def delete(self, key):  # pragma: no cover
        return True

    def get_source_info(self):
        return {
            "name": self._name,
            "available": self.is_available,  # bound method — truthy, not a bool
        }

    def proactive_check(self, context=None):  # pragma: no cover
        return {}

    def proactive_save(self, key, value, context=None):  # pragma: no cover
        return True


def test_gap014_failure_mode_is_documented():
    """Lock the observable consequence of the GAP014 regression so it's obvious
    when someone re-introduces it: ``available`` becomes a bound method, which
    is truthy but fails ``isinstance(v, bool)``.
    """
    src = _SyntheticMethodStyle()
    info = src.get_source_info()
    v = info["available"]
    # It IS truthy (which is why this bug was invisible in loose truthiness checks)
    assert bool(v) is True
    # ...but it is NOT a bool. That asymmetry is exactly the GAP014 signature.
    assert isinstance(v, types.MethodType), (
        f"Expected a bound method as the GAP014 signature, got {type(v).__name__}"
    )
    assert not isinstance(v, bool)
    # And it's not the literal ``True`` singleton either.
    assert v is not True


# --- 4. Property-style: the correct shape the test guards for --------------


class _SyntheticPropertyStyle(MemorySource):
    """A MemorySource that follows the contract (is_available as a property).

    This is the shape GAP014 was fixed to. ``available`` must be a bool.
    """

    def __init__(self):
        super().__init__("synthetic_property_style", None)
        self._name = "synthetic_property_style"
        self.config = {}
        self._available = True

    @property
    def is_available(self):
        return self._available

    def save(self, key, value):
        return True

    def retrieve(self, key):
        return None

    def search(self, query, limit=10):
        return []

    def delete(self, key):
        return True

    def get_source_info(self):
        return {
            "name": self._name,
            "available": self.is_available,  # property value — a real bool
        }

    def proactive_check(self, context=None):
        return {}

    def proactive_save(self, key, value, context=None):
        return True


def test_property_style_is_the_correct_shape():
    """The @property shape (post-GAP014) yields a real bool. This is the shape
    every real source follows and the shape this file guards for.
    """
    src = _SyntheticPropertyStyle()
    info = src.get_source_info()
    v = info["available"]
    assert isinstance(v, bool)
    assert v is True  # identity, not truthiness — the strict contract
    assert v is src.is_available
    # Flip the underlying flag and the bool should follow
    src._available = False
    info2 = src.get_source_info()
    assert info2["available"] is False
    assert info2["available"] is src.is_available
