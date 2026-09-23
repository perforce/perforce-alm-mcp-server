"""Test-case tools: get/create/update, search, steps, links, and attachments.

Search-body construction is exercised in depth in test_requirements.py (both
``*_by_query`` tools share ``_build_search_body``); here we just confirm the
test-case path/routing. The focus is the sub-object envelopes specific to test
cases (scriptsData, variantsData, stepsData) and the steps/links endpoints.
"""
import json
from unittest.mock import patch

import perforce_alm_mcp as alm
from _test_helpers import tool_fn

get_testcase = tool_fn("get_testcase")
get_testcases_by_query = tool_fn("get_testcases_by_query")
create_testcases = tool_fn("create_testcases")
update_testcases = tool_fn("update_testcases")
list_testcase_steps = tool_fn("list_testcase_steps")
update_testcase_steps = tool_fn("update_testcase_steps")
list_testcase_links = tool_fn("list_testcase_links")
create_testcase_links = tool_fn("create_testcase_links")
list_testcase_attachments = tool_fn("list_testcase_attachments")
upload_testcase_attachment = tool_fn("upload_testcase_attachment")


# --- get_testcases_by_query ---

def test_query_routes_to_testcases_search(project, token_req):
    get_testcases_by_query(filters={"Product": "X"})
    method, path = token_req.call_args.args
    assert (method, path) == ("POST", "/PROJ/testCases/search")
    assert token_req.call_args.kwargs["body"]["search"] == "\"Product\" = 'X'"


def test_query_default_args_send_empty_body(project, token_req):
    get_testcases_by_query()
    assert token_req.call_args.kwargs["body"] == {}


def test_query_error_returns_string(project, token_req):
    token_req.side_effect = RuntimeError("HTTP 400 Bad Request: bad field")
    assert get_testcases_by_query() == "Error: HTTP 400 Bad Request: bad field"


def test_query_strips_expanded_attachments_content(project, token_req):
    """expand=["attachments"] embeds the same AttachmentContainer shape
    list_testcase_attachments returns, including the dead `content` href —
    strip it here too, not just from the dedicated attachment tools."""
    token_req.return_value = {
        "testCases": [
            {
                "id": 7,
                "attachments": {
                    "attachmentsData": [{"id": 9, "encodedFileID": "abc", "content": "https://x/files/abc"}]
                },
            }
        ]
    }
    out = get_testcases_by_query()
    assert json.loads(out)["testCases"][0]["attachments"] == {
        "attachmentsData": [{"id": 9, "encodedFileID": "abc"}]
    }


# --- get_testcase ---

def test_get_testcase_path_without_params(project, token_req):
    token_req.return_value = {"id": 7}
    out = get_testcase(7)
    token_req.assert_called_once_with("GET", "/PROJ/testCases/7")
    assert json.loads(out) == {"id": 7}


def test_get_testcase_encodes_query_params(project, token_req):
    get_testcase(7, fields=["Summary"], formatted_text=False, expand=["steps", "links"])
    method, path = token_req.call_args.args
    assert method == "GET"
    assert path == "/PROJ/testCases/7?fields=Summary&formattedText=false&expand=steps&expand=links"


def test_get_testcase_strips_expanded_attachments_content(project, token_req):
    token_req.return_value = {
        "id": 7,
        "attachments": {
            "attachmentsData": [{"id": 9, "encodedFileID": "abc", "content": "https://x/files/abc"}]
        },
    }
    out = get_testcase(7, expand=["attachments"])
    assert json.loads(out) == {
        "id": 7,
        "attachments": {"attachmentsData": [{"id": 9, "encodedFileID": "abc"}]},
    }


# --- create_testcases ---

def test_create_testcases_wraps_all_subobjects(project, token_req_with_status):
    token_req_with_status.return_value = ({"testCases": [{"id": 1}]}, 201)
    create_testcases(
        fields=[{"label": "Summary", "type": "string", "string": "S"}],
        folders=[{"id": 1}],
        scripts=[{"id": 95, "referenceType": "attachment"}],
        variants={"included": [], "excluded": []},
        steps={"type": "detailed", "detailed": []},
    )
    method, path, body = token_req_with_status.call_args.args
    cases = body["testCases"]
    assert (method, path) == ("POST", "/PROJ/testCases")
    assert len(cases) == 1   # single-item arg wrapped into a length-1 bulk list
    tc = cases[0]
    assert tc["fields"] == [{"label": "Summary", "type": "string", "string": "S"}]
    assert tc["folders"] == {"foldersData": [{"id": 1}]}
    assert tc["scripts"] == {"scriptsData": [{"id": 95, "referenceType": "attachment"}]}
    assert tc["variants"] == {"variantsData": {"included": [], "excluded": []}}
    assert tc["steps"] == {"stepsData": {"type": "detailed", "detailed": []}}


def test_create_testcases_omits_unset_subobjects(project, token_req_with_status):
    create_testcases(fields=[{"label": "Summary", "type": "string", "string": "S"}])
    assert token_req_with_status.call_args.args[2]["testCases"][0] == {
        "fields": [{"label": "Summary", "type": "string", "string": "S"}]
    }


def test_create_testcases_206_with_folders_adds_warning(project, token_req_with_status):
    """A create can succeed (item assigned an id) while its `folders` sub-object
    fails, since folder placement is not atomic with the create either — same
    footgun as the update_* tools, so this tool prepends the same `warning` key."""
    token_req_with_status.return_value = (
        {"testCases": [{"id": 1}], "errors": [{"code": "E", "message": "bad folder", "statusCode": 404}]},
        206,
    )
    out = json.loads(create_testcases(
        fields=[{"label": "Summary", "type": "string", "string": "S"}],
        folders=[{"id": 999999}],
    ))
    assert "WARNING" in out["warning"]
    assert "folders" in out["warning"]
    assert out["testCases"] == [{"id": 1}]
    assert list(out.keys())[0] == "warning"  # prepended, not appended


def test_create_testcases_206_without_folders_has_no_warning(project, token_req_with_status):
    token_req_with_status.return_value = (
        {"testCases": [{"id": 1}], "errors": [{"code": "E", "message": "bad field", "statusCode": 400}]},
        206,
    )
    out = json.loads(create_testcases(fields=[{"label": "Summary", "type": "string", "string": "S"}]))
    assert "warning" not in out


# --- update_testcases ---

def test_update_testcases_wraps_subobjects_per_item(project, token_req_with_status):
    out = update_testcases([
        {
            "id": 10,
            "fields": [{"label": "Summary", "type": "string", "string": "S"}],
            "folders": [{"id": 2}],
            "scripts": [{"id": 5, "referenceType": "attachment"}],
            "variants": {"included": [], "excluded": []},
            "steps": {"type": "detailed", "detailed": []},
        }
    ])
    method, path, body = token_req_with_status.call_args.args
    item = body["testCases"][0]
    assert (method, path) == ("PUT", "/PROJ/testCases")
    assert item["id"] == 10
    assert item["folders"] == {"foldersData": [{"id": 2}]}
    assert item["scripts"] == {"scriptsData": [{"id": 5, "referenceType": "attachment"}]}
    assert item["variants"] == {"variantsData": {"included": [], "excluded": []}}
    assert item["steps"] == {"stepsData": {"type": "detailed", "detailed": []}}
    assert "warning" not in json.loads(out)  # plain 200, no partial-success signal


def test_update_testcases_206_with_folders_adds_warning(project, token_req_with_status):
    """On a 206 partial-success PUT that touched `folders`, the REST API's
    folders replace has already removed existing placements and applies
    every valid entry in the list regardless of an invalid entry elsewhere
    in it — so this tool prepends a `warning` key rather than passing the
    response through silently."""
    token_req_with_status.return_value = (
        {"testCases": [{"id": 10}], "errors": [{"code": "E", "message": "bad folder", "statusCode": 400}]},
        206,
    )
    out = json.loads(update_testcases([{"id": 10, "folders": [{"id": 999999}]}]))
    assert "WARNING" in out["warning"]
    assert "folders" in out["warning"]
    assert out["testCases"] == [{"id": 10}]
    assert list(out.keys())[0] == "warning"  # prepended, not appended


def test_update_testcases_206_without_folders_has_no_warning(project, token_req_with_status):
    """The partial-success warning is scoped to the folders footgun — a 206
    on a request that never touched `folders` shouldn't claim a folders risk
    that isn't there."""
    token_req_with_status.return_value = (
        {"testCases": [{"id": 10}], "errors": [{"code": "E", "message": "bad field", "statusCode": 400}]},
        206,
    )
    out = json.loads(update_testcases([{"id": 10, "fields": []}]))
    assert "warning" not in out


def test_update_testcases_rejects_attachments_key(project, token_req_with_status):
    """The REST update endpoint silently discards an attachments payload
    (verified empirically against a live server) — rejected here so the
    caller gets a targeted error instead of a false sense that the change
    took effect."""
    out = update_testcases([{"id": 10, "attachments": [{"id": 4}]}])
    assert out == (
        "Error: testcases[0] cannot include ['attachments'] in an update: "
        "attachments cannot be updated with PUT. Add them with upload_testcase_attachment."
    )
    token_req_with_status.assert_not_called()


def test_update_testcases_rejects_inline_links(project, token_req_with_status):
    """Inline link editing is intentionally not supported on update_testcases
    (a PUT replaces the whole link set wholesale — too easy to clobber). Links
    are managed via the dedicated *_testcase_links tools."""
    out = update_testcases([{"id": 10, "links": []}])
    assert out == (
        "Error: testcases[0] cannot include ['links'] in an update: "
        "links do not have an editing path. Add them with create_testcase_links "
        "instead of update_testcases."
    )
    token_req_with_status.assert_not_called()


def test_update_testcases_rejects_events_key(project, token_req_with_status):
    out = update_testcases([{"id": 10, "events": []}])
    assert out == (
        "Error: testcases[0] cannot include ['events'] in an update: "
        "events cannot be updated with PUT."
    )
    token_req_with_status.assert_not_called()


def test_update_testcases_rejects_unknown_keys(project, token_req_with_status):
    out = update_testcases([{"id": 1, "bogus": 1}])
    assert out == "Error: testcases[0] has unknown keys: ['bogus']"
    token_req_with_status.assert_not_called()


def test_update_testcases_missing_id_returns_error(project, token_req_with_status):
    """update_testcases pre-validates each item: a missing `id` returns an
    "Error: ..." string and never reaches the HTTP helper."""
    assert update_testcases([{"fields": []}]) == \
        "Error: testcases[0] missing the required 'id' key."
    token_req_with_status.assert_not_called()


# --- list/update_testcase_steps ---

def test_list_testcase_steps_path(project, token_req):
    token_req.return_value = {"stepsData": {"type": "detailed", "detailed": []}}
    out = list_testcase_steps(7)
    token_req.assert_called_once_with("GET", "/PROJ/testCases/7/steps")
    assert json.loads(out)["stepsData"]["type"] == "detailed"


def test_update_testcase_steps_wraps_stepsdata(project, token_req):
    steps = {"type": "detailed", "detailed": [{"type": "comment", "comment": "hi"}]}
    out = update_testcase_steps(7, steps)
    method, path = token_req.call_args.args
    assert (method, path) == ("PUT", "/PROJ/testCases/7/steps")
    assert token_req.call_args.kwargs["body"] == {"stepsData": steps}
    assert json.loads(out) == {"updated": True, "test_case_id": 7}   # 204 -> synthesized confirmation


# --- list/create_testcase_links ---

def test_list_testcase_links_path(project, token_req):
    token_req.return_value = {"linksData": []}
    list_testcase_links(7)
    token_req.assert_called_once_with("GET", "/PROJ/testCases/7/links")


def test_create_testcase_links_wraps_linksdata(project, token_req):
    links = [{"linkDefinition": {"id": 3}, "type": "peers",
              "peers": [{"itemID": 9, "itemType": "requirements"}]}]
    create_testcase_links(7, links)
    method, path = token_req.call_args.args
    assert (method, path) == ("POST", "/PROJ/testCases/7/links")
    assert token_req.call_args.kwargs["body"] == {"linksData": links}


# --- list/upload test-case attachments ---

def test_list_testcase_attachments_path(project, token_req):
    token_req.return_value = {
        "attachmentsData": [{"id": 5, "encodedFileID": "abc", "content": "https://x/files/abc"}]
    }
    out = list_testcase_attachments(7)
    token_req.assert_called_once_with("GET", "/PROJ/testCases/7/attachments")
    assert json.loads(out) == {"attachmentsData": [{"id": 5, "encodedFileID": "abc"}]}


def test_upload_testcase_attachment_delegates_to_multipart_helper(project):
    with patch.object(
        alm,
        "_upload_attachment_request",
        return_value={"attachmentsData": [{"id": 5, "encodedFileID": "abc", "content": "https://x/files/abc"}]},
    ) as up:
        out = upload_testcase_attachment(7, "C:/tmp/log.txt")
    up.assert_called_once_with("/PROJ/testCases/7/attachments", "C:/tmp/log.txt")
    assert json.loads(out) == {"attachmentsData": [{"id": 5, "encodedFileID": "abc"}]}


def test_upload_testcase_attachment_missing_file_returns_error(project, non_token_req, tmp_path, monkeypatch):
    monkeypatch.setenv("PERFORCE_ALM_MCP_UPLOAD_DIR", str(tmp_path))
    out = upload_testcase_attachment(7, str(tmp_path / "exist_xyz.bin"))
    assert out.startswith("Error:")
    assert "exist_xyz.bin" in out
    non_token_req.assert_not_called()  # the missing-file check must fire before any token/network call
