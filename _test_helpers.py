"""Importable test helpers for the Perforce ALM MCP server tests.

Kept separate from conftest.py: conftest is a pytest plugin module that pytest
auto-loads, and importing it directly (``from conftest import ...``) is
discouraged — under some rootdir/import-mode configurations it can be loaded
twice as distinct module objects. This module lives at the repo root (already on
sys.path, same as perforce_alm_mcp), so test modules can import it safely at
collection time.
"""
import perforce_alm_mcp as alm


def tool_fn(name: str):
    """Return the plain underlying callable for an ``@mcp.tool()``, robust to
    fastmcp's ``decorator_mode``.

    The server moved from the bundled ``mcp`` 1.27.1 FastMCP to the standalone
    ``fastmcp`` 3.x package (``from fastmcp import FastMCP``). Under mcp 1.27.1,
    ``@mcp.tool()`` returned the original function, so tests could call the tools
    directly. Under fastmcp 3.x the return value depends on
    ``fastmcp.settings.decorator_mode``:

    - ``"function"`` (the current default): the decorator returns the original
      function;
    - ``"object"``: it returns a ``FunctionTool`` whose ``.fn`` is the original
      function.

    ``getattr(obj, "fn", obj)`` yields the callable in either mode, so the suite
    keeps working if the mode ever flips. Call this at test-module import time and
    bind the result to a module-level name, e.g.
    ``get_requirement = tool_fn("get_requirement")`` — the tests reference those
    names directly. Caching at import is safe because ``decorator_mode`` is fixed
    at decoration/import time and never changes mid-run.
    """
    obj = getattr(alm, name)
    return getattr(obj, "fn", obj)
