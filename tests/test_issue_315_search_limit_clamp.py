#!/usr/bin/env python3
"""
test_issue_315_search_limit_clamp.py

Regression test for GH #315 — in-process recall injection was dead because
`_McpClient.search` passed its ``limit`` through verbatim to the
``mempalace_search`` MCP tool. MemPalace's MCP server caps search/list limit
at 100 (``mcp_server._MAX_RESULTS``); a larger value is rejected with
"Invalid params ... 'limit' must be at most 100", which tears down the
persistent MCP session and kills the ``on_pre_llm_call`` recall path.

Callers such as the lifecycle manager (``limit=10_000``) and the lifecycle
merge (``limit=500``) both exceeded the ceiling. The fix clamps ``limit``
inside ``_McpClient.search`` so *every* caller stays under the server
ceiling regardless of intent. This test pins that behaviour.
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from memchorus.mempalace_memory_source import _McpClient  # noqa: E402

# MemPalace MCP server's hard ceiling for search/list limit params.
SERVER_LIMIT_CEILING = 100


def _make_client():
    """A live _McpClient whose call_tool records args instead of hitting MCP."""
    client = _McpClient(timeout=1)
    client.calls = []
    client.call_tool = lambda name, args: client.calls.append((name, dict(args))) or []
    return client


class TestSearchLimitClamp:
    """_McpClient.search must clamp limit to the MCP server ceiling (#315)."""

    @pytest.mark.parametrize(
        "given,expected",
        [
            (10_000, SERVER_LIMIT_CEILING),   # lifecycle_manager's broad scan
            (500, SERVER_LIMIT_CEILING),      # lifecycle_merge's similar-scan
            (101, SERVER_LIMIT_CEILING),      # just over the ceiling
            (100, 100),                       # exactly the ceiling
            (50, 50),                         # under the ceiling, unchanged
            (5, 5),                           # the method default
            (1, 1),                           # minimum
        ],
    )
    def test_limit_clamped_to_ceiling(self, given, expected):
        client = _make_client()
        client.search("query", limit=given)
        assert client.calls, "call_tool should have been invoked"
        name, args = client.calls[0]
        assert name == "mempalace_search"
        assert args["limit"] == expected
        assert isinstance(args["limit"], int)
        assert args["limit"] <= SERVER_LIMIT_CEILING

    def test_invalid_limit_falls_back_to_default(self):
        client = _make_client()
        client.search("query", limit="big")  # non-numeric
        name, args = client.calls[0]
        assert args["limit"] == 5

    def test_zero_and_negative_clamp_to_one(self):
        client = _make_client()
        client.search("query", limit=0)
        assert client.calls[0][1]["limit"] == 1
        client.search("query", limit=-5)
        assert client.calls[1][1]["limit"] == 1

    def test_wing_room_still_forwarded(self):
        client = _make_client()
        client.search("query", limit=200, wing="wing_cthugha", room="aletheia")
        name, args = client.calls[0]
        assert name == "mempalace_search"
        assert args["limit"] == SERVER_LIMIT_CEILING
        assert args["wing"] == "wing_cthugha"
        assert args["room"] == "aletheia"
