"""Shared pytest fixtures for the Perforce ALM MCP server tests.

Lives at the project root so `import perforce_alm_mcp` resolves and so the
autouse fixture below isolates every test from the module-level global state
(`_config`, `_current_project`) that all the tools read and mutate.
(The `tool_fn` import helper lives in `_test_helpers.py`, not here, so test
modules don't have to import the conftest plugin module directly.)
"""
import copy
from unittest.mock import patch

import pytest

import perforce_alm_mcp as alm


@pytest.fixture(autouse=True)
def reset_state():
    """Snapshot and restore the module globals around every test.

    `_config` is mutated in place by the tools and `_current_project` is
    reassigned by select_project, so without this they would leak across tests.

    If a new mutable module-level global is ever added to perforce_alm_mcp (a
    cache, a compiled SSL context, etc.), add it here too — otherwise it will
    leak across tests with no failure pointing back to this fixture.
    """
    saved_config = copy.deepcopy(alm._config)
    saved_project = alm._current_project
    yield
    alm._config.clear()
    alm._config.update(saved_config)
    alm._current_project = saved_project


@pytest.fixture
def project():
    """Activate a project so token-scoped tools build paths and pass the gate.

    Pair this with `token_req` (or `non_token_req`): on its own it only sets
    `_current_project` and stubs nothing, so a token-scoped tool would fall
    through to the real `_token_authenticated_request` -> `_get_token()` and
    attempt a live network call.

    The autouse `reset_state` fixture restores `_current_project` afterward.
    """
    alm._current_project = "PROJ"
    return "PROJ"


@pytest.fixture
def token_req():
    """Patch the Bearer-token request helper that every project-scoped tool calls.

    Yields the mock; default return is ``{}`` (which flows straight through
    ``json.dumps`` into the tool's return value, so an un-overridden mock asserts
    against an empty-envelope response). Set ``.return_value`` to supply a canned
    REST API response, or ``.side_effect`` to simulate a failure. Assert the
    request the tool built via ``m.call_args`` (``method, path`` positional;
    ``body`` keyword).
    """
    with patch.object(alm, "_token_authenticated_request", return_value={}) as m:
        yield m


@pytest.fixture
def token_req_with_status():
    """Patch the status-aware Bearer-token request helper used by the four
    update_* tools (update_requirements, update_issues, update_testcases,
    update_documents) and the four create_* tools (create_requirements,
    create_issues, create_testcases, create_documents) to detect a 206
    partial-success PUT/POST — see ``_folders_partial_success_warning`` in
    perforce_alm_mcp.py.

    Yields the mock; default return is ``({}, 200)`` (a plain success with no
    partial-success signal). Set ``.return_value`` to ``(data, status)`` to
    simulate a specific response, e.g. ``(data, 206)`` or
    ``({**data, "errors": [...]}, 200)``. Assert the request the tool built
    via ``m.call_args`` (``method, path, body`` all positional — this helper
    takes no keyword args).
    """
    with patch.object(alm, "_token_authenticated_request_with_status", return_value=({}, 200)) as m:
        yield m


@pytest.fixture
def non_token_req():
    """Patch the API-key/basic request helper used for ``/projects``,
    ``/versions``, and ``/{projectID}/token``.

    Yields the mock; default return is ``{}`` (which flows straight through
    ``json.dumps`` into the tool's return value). Set ``.return_value`` to supply
    a canned response, or ``.side_effect`` to simulate a failure. Assert the
    request via ``m.call_args`` (``method, path`` positional; this helper takes no
    ``body``).
    """
    with patch.object(alm, "_non_token_authenticated_request", return_value={}) as m:
        yield m
