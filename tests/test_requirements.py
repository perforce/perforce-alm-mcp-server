"""Requirement tools: get/create/update, search, links, and attachments.

Covers search-expression building, query-param encoding, the project-segment
percent-encoding done by ``_project_path``, sub-object envelope wrapping, the
single-item-wrap behavior of ``create_*``, the per-item validation guards on
``update_requirements``, and the success/error string contract.
"""
import json
import urllib.error
from unittest.mock import patch

import perforce_alm_mcp as alm
from _test_helpers import tool_fn

get_requirement = tool_fn("get_requirement")
get_requirements_by_query = tool_fn("get_requirements_by_query")
create_requirements = tool_fn("create_requirements")
update_requirements = tool_fn("update_requirements")
list_requirement_links = tool_fn("list_requirement_links")
create_requirement_links = tool_fn("create_requirement_links")
list_requirement_attachments = tool_fn("list_requirement_attachments")
upload_requirement_attachment = tool_fn("upload_requirement_attachment")


# --- get_requirements_by_query: search-body construction ---

def test_filters_build_quoted_search_expression(project, token_req):
    token_req.return_value = {"requirements": []}
    get_requirements_by_query(filters={"Product": "WysiCorp", "Multi Word": "x"})

    method, path = token_req.call_args.args
    body = token_req.call_args.kwargs["body"]
    assert method == "POST"
    assert path == "/PROJ/requirements/search"
    assert body["search"] == "\"Product\" = 'WysiCorp' and \"Multi Word\" = 'x'"


def test_filters_wrap_value_with_single_quote_in_double_quotes(project, token_req):
    # No quote-doubling: a value containing ' is wrapped in double quotes instead.
    get_requirements_by_query(filters={"Name": "O'Brien"})
    assert token_req.call_args.kwargs["body"]["search"] == "\"Name\" = \"O'Brien\""


def test_filters_and_raw_search_are_and_joined_with_parens(project, token_req):
    get_requirements_by_query(filters={"Product": "A"}, search="Summary contains 'x'")
    assert (
        token_req.call_args.kwargs["body"]["search"]
        == "\"Product\" = 'A' and (Summary contains 'x')"
    )


def test_default_args_send_empty_body(project, token_req):
    get_requirements_by_query()
    # page/per_page/formatted_text at their defaults must be omitted.
    assert token_req.call_args.kwargs["body"] == {}


def test_non_default_paging_and_fields_included(project, token_req):
    get_requirements_by_query(
        fields=["Summary"], page=2, per_page=50, formatted_text=False, expand=["links"]
    )
    body = token_req.call_args.kwargs["body"]
    assert body == {
        "fields": ["Summary"],
        "page": 2,
        "per_page": 50,
        "formattedText": False,
        "expand": ["links"],
    }


# --- shape contract (success / error) ---

def test_success_returns_indented_json(project, token_req):
    payload = {"requirements": [{"id": 1}]}
    token_req.return_value = payload
    out = get_requirements_by_query()
    assert out == json.dumps(payload, indent=2)


def test_failure_returns_error_string_not_json(project, token_req):
    token_req.side_effect = RuntimeError("HTTP 400 Bad Request: nope")
    out = get_requirements_by_query()
    assert out == "Error: HTTP 400 Bad Request: nope"
    assert not out.lstrip().startswith("{")


def test_connection_failure_surfaces_as_error_string(project, token_req):
    """REST API down / unreachable -> URLError -> tool's outer guard -> 'Error: ...'."""
    token_req.side_effect = urllib.error.URLError("Connection refused")
    out = get_requirements_by_query()
    assert out.startswith("Error:")
    assert "Connection refused" in out


# --- _project_path: the active-project segment is percent-encoded ---

def test_project_segment_is_percent_encoded(token_req):
    alm._current_project = "My Project"   # space must not break the URL path
    get_requirements_by_query()
    _, path = token_req.call_args.args
    assert path == "/My%20Project/requirements/search"


# --- get_requirement ---

def test_get_requirement_builds_path_without_params(project, token_req):
    token_req.return_value = {"id": 3}
    out = get_requirement(3)
    token_req.assert_called_once_with("GET", "/PROJ/requirements/3")
    assert json.loads(out) == {"id": 3}


def test_get_requirement_encodes_query_params(project, token_req):
    get_requirement(
        3, fields=["Summary", "Product"], formatted_text=False, expand=["links"], version=2
    )
    method, path = token_req.call_args.args
    assert method == "GET"
    assert path == (
        "/PROJ/requirements/3?fields=Summary&fields=Product"
        "&formattedText=false&expand=links&version=2"
    )


def test_get_requirement_strips_expanded_attachments_content(project, token_req):
    """expand=["attachments"] embeds the same AttachmentContainer shape
    list_requirement_attachments returns, including the dead `content` href —
    strip it here too, not just from the dedicated attachment tools."""
    token_req.return_value = {
        "id": 3,
        "attachments": {
            "attachmentsData": [{"id": 9, "encodedFileID": "abc", "content": "https://x/files/abc"}]
        },
    }
    out = get_requirement(3, expand=["attachments"])
    assert json.loads(out) == {
        "id": 3,
        "attachments": {"attachmentsData": [{"id": 9, "encodedFileID": "abc"}]},
    }


def test_get_requirements_by_query_strips_expanded_attachments_content(project, token_req):
    token_req.return_value = {
        "requirements": [
            {
                "id": 3,
                "attachments": {
                    "attachmentsData": [{"id": 9, "encodedFileID": "abc", "content": "https://x/files/abc"}]
                },
            }
        ]
    }
    out = get_requirements_by_query()
    assert json.loads(out)["requirements"][0]["attachments"] == {
        "attachmentsData": [{"id": 9, "encodedFileID": "abc"}]
    }


# --- create_requirements ---

def test_create_requirements_wraps_envelopes(project, token_req_with_status):
    token_req_with_status.return_value = ({"requirements": [{"id": 1}]}, 201)
    create_requirements(
        fields=[{"id": 1, "type": "string", "string": "S"}],
        requirement_type={"id": 7},
        folders=[{"id": 2}],
    )
    method, path, body = token_req_with_status.call_args.args
    reqs = body["requirements"]
    assert (method, path) == ("POST", "/PROJ/requirements")
    assert len(reqs) == 1   # single-item arg is wrapped into a length-1 bulk list
    req = reqs[0]
    assert req["fields"] == [{"id": 1, "type": "string", "string": "S"}]
    assert req["requirementType"] == {"id": 7}
    assert req["folders"] == {"foldersData": [{"id": 2}]}


def test_create_requirements_omits_unset_optionals(project, token_req_with_status):
    create_requirements(fields=[{"id": 1, "type": "boolean", "boolean": True}])
    assert token_req_with_status.call_args.args[2]["requirements"][0] == {
        "fields": [{"id": 1, "type": "boolean", "boolean": True}]
    }


def test_create_requirements_206_with_folders_adds_warning(project, token_req_with_status):
    """A create can succeed (item assigned an id) while its `folders` sub-object
    fails, since folder placement is not atomic with the create either — same
    footgun as the update_* tools, so this tool prepends the same `warning` key."""
    token_req_with_status.return_value = (
        {"requirements": [{"id": 1}], "errors": [{"code": "E", "message": "bad folder", "statusCode": 404}]},
        206,
    )
    out = json.loads(create_requirements(
        fields=[{"id": 1, "type": "string", "string": "S"}],
        requirement_type={"id": 7},
        folders=[{"id": 999999}],
    ))
    assert "WARNING" in out["warning"]
    assert "folders" in out["warning"]
    assert out["requirements"] == [{"id": 1}]
    assert list(out.keys())[0] == "warning"  # prepended, not appended


def test_create_requirements_206_without_folders_has_no_warning(project, token_req_with_status):
    token_req_with_status.return_value = (
        {"requirements": [{"id": 1}], "errors": [{"code": "E", "message": "bad field", "statusCode": 400}]},
        206,
    )
    out = json.loads(create_requirements(fields=[{"id": 1, "type": "boolean", "boolean": True}]))
    assert "warning" not in out


# --- update_requirements ---

def test_update_requirements_wraps_subobjects_per_item(project, token_req_with_status):
    out = update_requirements([
        {
            "id": 10,
            "fields": [{"id": 1, "type": "string", "string": "S"}],
            "folders": [{"id": 2}],
            "requirementType": {"id": 7},
        }
    ])
    method, path, body = token_req_with_status.call_args.args
    item = body["requirements"][0]
    assert (method, path) == ("PUT", "/PROJ/requirements")
    assert item["id"] == 10
    assert item["folders"] == {"foldersData": [{"id": 2}]}
    assert item["requirementType"] == {"id": 7}
    assert "warning" not in json.loads(out)  # plain 200, no partial-success signal


def test_update_requirements_206_with_folders_adds_warning(project, token_req_with_status):
    """On a 206 partial-success PUT that touched `folders`, the REST API's
    folders replace has already removed existing placements and applies
    every valid entry in the list regardless of an invalid entry elsewhere
    in it — so this tool prepends a `warning` key rather than passing the
    response through silently."""
    token_req_with_status.return_value = (
        {"requirements": [{"id": 10}], "errors": [{"code": "E", "message": "bad folder", "statusCode": 400}]},
        206,
    )
    out = json.loads(update_requirements([{"id": 10, "folders": [{"id": 999999}]}]))
    assert "WARNING" in out["warning"]
    assert "folders" in out["warning"]
    assert out["requirements"] == [{"id": 10}]
    assert out["errors"][0]["code"] == "E"
    assert list(out.keys())[0] == "warning"  # prepended, not appended


def test_update_requirements_206_without_folders_has_no_warning(project, token_req_with_status):
    """The partial-success warning is scoped to the folders footgun — a 206
    on a request that never touched `folders` shouldn't claim a folders risk
    that isn't there."""
    token_req_with_status.return_value = (
        {"requirements": [{"id": 10}], "errors": [{"code": "E", "message": "bad field", "statusCode": 400}]},
        206,
    )
    out = json.loads(update_requirements([{"id": 10, "fields": []}]))
    assert "warning" not in out


def test_update_requirements_rejects_inline_links(project, token_req_with_status):
    """Inline link editing is intentionally not supported on update_requirements
    (a PUT replaces the whole link set wholesale — too easy to clobber). Links
    are managed via the dedicated *_requirement_links tools."""
    out = update_requirements([{"id": 10, "links": []}])
    assert out == (
        "Error: requirements[0] cannot include ['links'] in an update: "
        "links do not have an editing path. Add them with create_requirement_links "
        "instead of update_requirements."
    )
    token_req_with_status.assert_not_called()


def test_update_requirements_rejects_events_key(project, token_req_with_status):
    out = update_requirements([{"id": 10, "events": []}])
    assert out == (
        "Error: requirements[0] cannot include ['events'] in an update: "
        "events cannot be updated with PUT."
    )
    token_req_with_status.assert_not_called()


def test_update_requirements_rejects_attachments_key(project, token_req_with_status):
    """The REST update endpoint silently discards an attachments payload
    (verified empirically against a live server) — rejected here so the
    caller gets a targeted error instead of a false sense that the change
    took effect."""
    out = update_requirements([{"id": 10, "attachments": [{"id": 4}]}])
    assert out == (
        "Error: requirements[0] cannot include ['attachments'] in an update: "
        "attachments cannot be updated with PUT. Add them with upload_requirement_attachment."
    )
    token_req_with_status.assert_not_called()


def test_update_requirements_rejects_item_missing_id(project, token_req_with_status):
    out = update_requirements([{"fields": []}])
    assert out == "Error: requirements[0] missing the required 'id' key."
    token_req_with_status.assert_not_called()  # validation fails before any HTTP call


def test_update_requirements_rejects_unknown_keys(project, token_req_with_status):
    out = update_requirements([{"id": 1, "bogus": 1}])
    assert out == "Error: requirements[0] has unknown keys: ['bogus']"
    token_req_with_status.assert_not_called()


# --- list/create_requirement_links ---

def test_list_requirement_links_path(project, token_req):
    token_req.return_value = {"linksData": []}
    list_requirement_links(7)
    token_req.assert_called_once_with("GET", "/PROJ/requirements/7/links")


def test_create_requirement_links_wraps_linksdata(project, token_req):
    links = [{"linkDefinition": {"id": 3}, "type": "peers",
              "peers": [{"itemID": 9, "itemType": "testCases"}]}]
    create_requirement_links(7, links)
    method, path = token_req.call_args.args
    assert (method, path) == ("POST", "/PROJ/requirements/7/links")
    assert token_req.call_args.kwargs["body"] == {"linksData": links}


# --- list/upload requirement attachments ---

def test_list_requirement_attachments_path(project, token_req):
    token_req.return_value = {
        "attachmentsData": [{"id": 9, "encodedFileID": "abc", "content": "https://x/files/abc"}]
    }
    out = list_requirement_attachments(7)
    token_req.assert_called_once_with("GET", "/PROJ/requirements/7/attachments")
    assert json.loads(out) == {"attachmentsData": [{"id": 9, "encodedFileID": "abc"}]}


def test_upload_requirement_attachment_delegates_to_multipart_helper(project):
    with patch.object(
        alm,
        "_upload_attachment_request",
        return_value={"attachmentsData": [{"id": 9, "encodedFileID": "abc", "content": "https://x/files/abc"}]},
    ) as up:
        out = upload_requirement_attachment(7, "C:/tmp/spec.pdf")
    up.assert_called_once_with("/PROJ/requirements/7/attachments", "C:/tmp/spec.pdf")
    assert json.loads(out) == {"attachmentsData": [{"id": 9, "encodedFileID": "abc"}]}


def test_upload_requirement_attachment_missing_file_returns_error(project, non_token_req, tmp_path, monkeypatch):
    monkeypatch.setenv("PERFORCE_ALM_MCP_UPLOAD_DIR", str(tmp_path))
    out = upload_requirement_attachment(7, str(tmp_path / "exist_xyz.bin"))
    assert out.startswith("Error:")
    assert "exist_xyz.bin" in out
    non_token_req.assert_not_called()  # the missing-file check must fire before any token/network call
