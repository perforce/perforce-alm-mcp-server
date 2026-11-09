"""Tests for resolving user-facing requirement identifiers."""
import json

from _test_helpers import tool_fn

import perforce_alm_mcp as alm

get_requirement_by_identifier = tool_fn("get_requirement_by_identifier")


def test_numeric_internal_id_is_fetched_directly(project, token_req):
    token_req.return_value = {"id": 1960, "tag": "BR-42"}

    result = get_requirement_by_identifier("1960")

    token_req.assert_called_once_with("GET", "/PROJ/requirements/1960")
    assert json.loads(result)["id"] == 1960


def test_tag_is_resolved_with_search_and_then_fetched(project, token_req):
    token_req.side_effect = [
        {"requirements": [{"id": 77, "tag": "BR-1960"}]},
        {"id": 77, "tag": "BR-1960"},
    ]

    result = get_requirement_by_identifier("BR-1960")

    assert token_req.call_args_list[0].args == ("POST", "/PROJ/requirements/search")
    assert token_req.call_args_list[0].kwargs["body"] == {
        "search": "Tag contains '1960'",
        "fields": ["Tag"],
    }
    assert token_req.call_args_list[1].args == ("GET", "/PROJ/requirements/77")
    assert json.loads(result)["id"] == 77


def test_identifier_is_trimmed(project, token_req):
    token_req.return_value = {"id": 77, "tag": "BR-1960"}

    get_requirement_by_identifier("  BR-1960  ")

    assert token_req.call_args_list[0].kwargs["body"]["search"] == "Tag contains '1960'"


def test_unknown_identifier_returns_clear_error(project, token_req):
    token_req.return_value = {"requirements": []}

    result = get_requirement_by_identifier("BR-9999")

    assert result == "Error: requirement 'BR-9999' was not found."
