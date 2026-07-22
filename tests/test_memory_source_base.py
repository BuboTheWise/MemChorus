#!/usr/bin/env python3
"""
test_memory_source_base.py - Test the MemorySource abstract base class.

Tests:
1. Instantiating MemorySource raises TypeError (abstract)
2. Concrete subclasses can be created
3. All abstract methods are required and enforced by ABC machinery
4. Abstract method signatures work correctly with concrete implementations
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from memchorus.memory_source import MemorySource


def test_memory_source_is_abstract_and_cannot_be_instantiated():
    """Attempting to instantiate MemorySource raises TypeError because it is abstract."""
    with pytest.raises(TypeError):
        MemorySource()  # Should fail due to unimplemented abstract methods


def test_memory_source_subclass_without_implementation_still_fails():
    """Even a subclass that does not implement all abstract methods still fails instantiation."""

    class PartialMemorySource(MemorySource):
        def __init__(self, name="partial_test"):
            self.name = name

        # Only implemented one method — others missing
        def save(self, key, value):
            return True

    with pytest.raises(TypeError):
        PartialMemorySource()  # Fails because other abstract methods are unimplemented


def test_memory_source_subclass_with_all_methods_is_instantiable():
    """A subclass that implements all required methods creates a working instance."""

    class ConcreteMemorySource(MemorySource):
        SUPPORTED_METHODS = ['save', 'retrieve', 'search', 'get_source_info']

        def __init__(self, name="concrete_test", config=None):
            self.name = name

        def is_available(self):
            return True

        def save(self, key, value):
            return True

        def retrieve(self, key):
            return None

        def search(self, query, limit=10):
            return []

        def delete(self, key):
            return True

        def get_source_info(self):
            return {"name": self.name}

        def proactive_check(self, context=None):
            return {}

        def proactive_save(self, key, value, context=None):
            pass

    src = ConcreteMemorySource()
    assert src is not None
    assert isinstance(src, MemorySource)


def test_memory_source_instance_has_name_attribute():
    """A MemorySource that has been properly instantiated always has name attribute."""

    class TestableMemorySource(MemorySource):
        def __init__(self, name="test", config=None):
            self.name = name

        def is_available(self):
            return True

        def save(self, key, value):
            return True

        def retrieve(self, key):
            return None

        def search(self, query, limit=10):
            return []

        def delete(self, key):
            return True

        def get_source_info(self):
            return {"name": self.name}

        def proactive_check(self, context=None):
            return {}

        def proactive_save(self, key, value, context=None):
            pass

    src = TestableMemorySource(name="custom_name")
    assert src.name == "custom_name"


def test_memory_source_name_is_string():
    """name attribute on a concrete MemorySource should be a string."""

    class SimpleMemorySource(MemorySource):
        def __init__(self, name="simple", config=None):
            self.name = name

        def is_available(self):
            return True

        def save(self, key, value):
            return True

        def retrieve(self, key):
            return None

        def search(self, query, limit=10):
            return []

        def delete(self, key):
            return True

        def get_source_info(self):
            return {"name": self.name}

        def proactive_check(self, context=None):
            return {}

        def proactive_save(self, key, value, context=None):
            pass

    src = SimpleMemorySource()
    assert isinstance(src.name, str)


def test_memory_source_is_subclass():
    """Concrete implementations should be subclasses of MemorySource."""

    from memchorus.orchestrator import MemoryOrchestrator
    from memchorus.hermes_memory_source import HermesDefaultMemorySource
    from memchorus.mempalace_memory_source import MemPalaceMemorySource

    # Re-import MemorySource fresh to avoid dual-namespace collision under xdist.
    from memchorus.memory_source import MemorySource as _MemorySource  # noqa: F811
    assert issubclass(HermesDefaultMemorySource, _MemorySource)
    assert issubclass(MemPalaceMemorySource, _MemorySource)


def test_hermes_default_and_mempalace_are_instances():
    """Concrete source classes are valid instances of MemorySource."""
    tmpdir = os.path.join(os.path.dirname(__file__), '..', 'src')
    sys.path.insert(0, tmpdir)

    from memchorus.hermes_memory_source import HermesDefaultMemorySource
    from memchorus.mempalace_memory_source import MemPalaceMemorySource
    from memchorus.memory_source import MemorySource as _MemorySource  # fresh ref
    import tempfile

    d = tempfile.mkdtemp(prefix="mem_src_test_")
    try:
        hermes_src = HermesDefaultMemorySource(name="test", config={"memory_dir": d})
        assert isinstance(hermes_src, _MemorySource)

        # skip_mcp=True so it doesn't try to connect to a live MCP server in tests
        mp_src = MemPalaceMemorySource(config={"skip_mcp": True})
        assert isinstance(mp_src, _MemorySource)
    finally:
        import shutil; shutil.rmtree(d, ignore_errors=True)

