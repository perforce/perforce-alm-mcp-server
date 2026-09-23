"""Tool-agnostic audit of every tool's ``annotations=`` argument.

Follows the same AST-scan pattern as ``test_json_alias_audit.py``: enumerating
via ``mcp.get_tools()`` would reflect any enable/disable visibility transform
applied to a tool, silently dropping it from the audit. Scanning the source
for the decorator finds every tool regardless of visibility state.

The assertion is semantic, not name-based: it checks that the ``annotations=``
keyword resolves to one of the ``ToolAnnotations`` instances actually defined
in the module, not that it spells one of today's nine constant names. A new
constant added later (following the pattern described in the module's
docstring, "add a new constant following the same pattern rather than reaching
for the closest existing one") therefore passes with no change here, while a
missing ``annotations=`` argument, or one bound to something else, fails.
"""
import ast
import inspect

from mcp.types import ToolAnnotations

import perforce_alm_mcp as alm


def _tool_decorators():
    """Yield ``(function_name, decorator_call_node)`` for every ``@mcp.tool()``."""
    tree = ast.parse(inspect.getsource(alm))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in node.decorator_list:
            if isinstance(d, ast.Call) and getattr(d.func, "attr", None) == "tool":
                yield node.name, d


def _valid_annotation_names():
    """Every module-level name bound to a ``ToolAnnotations`` instance."""
    return {name for name, value in vars(alm).items() if isinstance(value, ToolAnnotations)}


def _annotations_keyword(call_node):
    """Return the ``annotations=`` keyword's value node, or ``None`` if absent."""
    for kw in call_node.keywords:
        if kw.arg == "annotations":
            return kw.value
    return None


def _audit():
    """Return ``(offenders, checked_count)`` across every tool."""
    valid_names = _valid_annotation_names()
    offenders, checked = [], 0
    for tool_name, call_node in _tool_decorators():
        checked += 1
        value_node = _annotations_keyword(call_node)
        if value_node is None:
            offenders.append(f"{tool_name}: missing annotations= argument")
        elif not (isinstance(value_node, ast.Name) and value_node.id in valid_names):
            offenders.append(
                f"{tool_name}: annotations= is not bound to a recognized "
                f"ToolAnnotations constant"
            )
    return offenders, checked


# --- the audit itself ---

def test_every_tool_has_a_recognized_annotations_constant():
    offenders, checked = _audit()
    assert not offenders, (
        "These @mcp.tool() functions are missing annotations= (or it isn't "
        "bound to one of the ToolAnnotations constants defined near the top "
        "of perforce_alm_mcp.py), so an MCP client cannot tell a read from a "
        "bulk write or an automation trigger when deciding whether a tool "
        "call needs human approval. Pick the closest existing constant, or "
        "add a new one following the same pattern if none fit:\n  "
        + "\n  ".join(offenders)
    )
    # Guard against a vacuous pass: if the AST scan silently breaks, `offenders`
    # would also be empty. Deliberately a floor, not an exact count, so simply
    # adding a tool doesn't require touching this number.
    assert checked >= 40, (
        f"only {checked} @mcp.tool() functions were examined; expected 40+. "
        "The enumeration logic has probably broken, which would make this "
        "test pass without checking anything."
    )


# --- controls on the detector, so the audit above cannot pass vacuously ---

_SAMPLE_SOURCE_OK = """
@mcp.tool(annotations=_READ_ONLY)
def sample_ok():
    pass
"""

_SAMPLE_SOURCE_MISSING = """
@mcp.tool()
def sample_missing():
    pass
"""

_SAMPLE_SOURCE_UNRECOGNIZED = """
@mcp.tool(annotations=_NOT_A_REAL_CONSTANT)
def sample_unrecognized():
    pass
"""


def _decorators_from_source(source):
    tree = ast.parse(source)
    return [
        (node.name, d)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for d in node.decorator_list
        if isinstance(d, ast.Call) and getattr(d.func, "attr", None) == "tool"
    ]


def test_detector_accepts_a_recognized_constant():
    valid_names = _valid_annotation_names()
    ((name, call_node),) = _decorators_from_source(_SAMPLE_SOURCE_OK)
    value_node = _annotations_keyword(call_node)
    assert isinstance(value_node, ast.Name) and value_node.id in valid_names


def test_detector_flags_a_missing_annotations_argument():
    ((name, call_node),) = _decorators_from_source(_SAMPLE_SOURCE_MISSING)
    assert _annotations_keyword(call_node) is None


def test_detector_flags_an_unrecognized_constant():
    valid_names = _valid_annotation_names()
    ((name, call_node),) = _decorators_from_source(_SAMPLE_SOURCE_UNRECOGNIZED)
    value_node = _annotations_keyword(call_node)
    assert isinstance(value_node, ast.Name) and value_node.id not in valid_names


def test_real_constants_are_actually_found():
    # Sanity check that the eight documented constants really resolve as
    # ToolAnnotations instances, so the "valid_names" set isn't accidentally empty.
    valid_names = _valid_annotation_names()
    for name in (
        "_READ_ONLY",
        "_READ_ONLY_LOCAL",
        "_SESSION_STATE",
        "_WRITE_ADDITIVE",
        "_WRITE_ASSOCIATIVE",
        "_WRITE_DESTRUCTIVE",
        "_WRITE_DESTRUCTIVE_UNSAFE_RETRY",
        "_LOCAL_FILE_WRITE",
        "_TRIGGER",
    ):
        assert name in valid_names
