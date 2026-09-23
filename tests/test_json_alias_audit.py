"""Tool-agnostic audit of every tool's parameter annotations.

Complements ``tests/test_json_coercion.py``. That file exercises the coercion
*behavior* tool-by-tool, which means it only covers tools someone remembered to
add a case for — it cannot fail for a *newly added* tool whose optional
array/object parameter forgot the ``Json*`` alias. This file inverts that: it
walks every ``@mcp.tool()`` in the module and asserts the rule structurally, so
the omission fails the suite the moment the tool is written.

Two deliberate design choices:

- **Enumeration is by AST scan of the source, not the FastMCP registry.**
  ``mcp.get_tools()`` would reflect any enable/disable visibility transform
  applied to a tool, silently dropping it from the audit. Scanning for the
  decorator finds every tool regardless of visibility state, and needs no
  fastmcp internals.
- **The assertion is semantic, not name-based.** It checks that the annotation
  actually carries a ``BeforeValidator`` bound to ``_coerce_json`` — not that it
  spells one of today's five alias names. A new alias (e.g. a ``JsonIntList``
  for ``list[int] | None``) therefore passes with no change here, while a bare
  ``list[int] | None`` fails.
"""
import ast
import inspect
import types
import typing

import pytest
from pydantic import BeforeValidator

import perforce_alm_mcp as alm
from _test_helpers import tool_fn


# Only list/dict/set/tuple annotations are affected. A scalar (`str | None`,
# `int | None`) is marshalled correctly by the client even when wrapped in
# `anyOf`, so those need no alias.
_CONTAINER_TYPES = (list, dict, set, tuple)


def _tool_names():
    """Every ``@mcp.tool()``-decorated function name, read from the source."""
    tree = ast.parse(inspect.getsource(alm))
    names = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(d, ast.Call) and getattr(d.func, "attr", None) == "tool"
            for d in node.decorator_list
        )
    ]
    assert names, "found no @mcp.tool() functions — the AST scan is broken"
    return names


def _unwrap(annotation):
    """Strip ``Annotated[...]`` down to the underlying type."""
    return annotation.__origin__ if hasattr(annotation, "__metadata__") else annotation


def _is_container(annotation):
    base = _unwrap(annotation)
    origin = typing.get_origin(base) or base
    return isinstance(origin, type) and issubclass(origin, _CONTAINER_TYPES)


def _coerces_json(annotation):
    """True if the annotation carries a ``BeforeValidator(_coerce_json)``."""
    return any(
        isinstance(meta, BeforeValidator) and meta.func is alm._coerce_json
        for meta in getattr(annotation, "__metadata__", ())
    )


def _optional_container_params(fn):
    """Yield ``(param_name, branch)`` for each ``<container> | None`` parameter.

    Gated on the union-with-``None`` specifically, because that is the exact
    trigger for the bug: it is what makes pydantic emit ``anyOf`` and drop the
    top-level ``"type"`` key. A required container param — or one with a default
    but no ``| None`` — advertises its type directly and is unaffected.
    """
    hints = typing.get_type_hints(fn, include_extras=True)
    for name, param in inspect.signature(fn).parameters.items():
        if param.default is inspect.Parameter.empty:
            continue
        annotation = hints.get(name)
        if typing.get_origin(annotation) not in (typing.Union, types.UnionType):
            continue
        branches = typing.get_args(annotation)
        if type(None) not in branches:
            continue
        for branch in branches:
            if branch is not type(None) and _is_container(branch):
                yield name, branch


def _audit():
    """Return ``(offenders, checked_count)`` across every tool."""
    offenders, checked = [], 0
    for tool_name in _tool_names():
        fn = tool_fn(tool_name)
        for param_name, branch in _optional_container_params(fn):
            checked += 1
            if not _coerces_json(branch):
                offenders.append(f"{tool_name}({param_name}: {branch!r})")
    return offenders, checked


# --- the audit itself ---

def test_every_optional_container_param_uses_a_coercing_alias():
    offenders, checked = _audit()
    assert not offenders, (
        "These optional array/object tool parameters are annotated with a bare "
        "type instead of a Json* alias, so at least one real MCP client (Claude "
        "Code) will send them JSON-stringified and pydantic will reject the "
        "call. Use the matching alias from perforce_alm_mcp — or, if none "
        "matches the element type you need, add one following the same "
        "Annotated[<type>, BeforeValidator(_coerce_json)] pattern:\n  "
        + "\n  ".join(offenders)
    )
    # Guard against a vacuous pass: if enumeration or detection silently breaks,
    # `offenders` would also be empty. Deliberately a floor, not an exact count,
    # so simply adding a tool doesn't require touching this number.
    assert checked >= 40, (
        f"only {checked} optional container params were examined; expected 40+. "
        "The enumeration or detection logic has probably broken, which would "
        "make this test pass without checking anything."
    )


# --- controls on the detector, so the audit above cannot pass vacuously ---

def _sample_bare(param: list[int] | None = None):
    """Stand-in for the mistake this file exists to catch."""


def _sample_aliased(param: alm.JsonStrList | None = None):
    """Stand-in for the correct form."""


def _sample_required(param: dict):
    """Required container: renders with a top-level "type", needs no alias."""


def _sample_scalar(param: str | None = None):
    """Optional scalar: unaffected by the anyOf marshalling quirk."""


def test_detector_flags_a_bare_optional_container():
    found = dict(_optional_container_params(_sample_bare))
    assert "param" in found
    assert not _coerces_json(found["param"])


def test_detector_accepts_an_aliased_optional_container():
    found = dict(_optional_container_params(_sample_aliased))
    assert "param" in found
    assert _coerces_json(found["param"])


@pytest.mark.parametrize("fn", [_sample_required, _sample_scalar])
def test_detector_ignores_params_the_rule_does_not_cover(fn):
    assert not list(_optional_container_params(fn))
