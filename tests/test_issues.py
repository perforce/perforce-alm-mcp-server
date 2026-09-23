"""Issue tools: get/create/update, search, links, and attachments.

Covers single-item query-param encoding, search-body construction (shared with
the other ``*_by_query`` tools via ``_build_search_body``), the project-scoped
``/issues/{itemID}``, ``/issues/search``, and ``/issues`` paths, sub-object
envelope wrapping on create/update, the per-item validation guards on
``update_issues`` (required ``id``, not-PUT-updatable and unknown keys), and the
success/error string contract. The search-expression edge cases (quote handling,
and-joining) are exercised in depth against ``get_requirements_by_query`` in
test_requirements.py; here we confirm issues route to their own endpoints and
honor the same body-building and shape conventions.
"""
import json
import urllib.error
from unittest.mock import patch

import perforce_alm_mcp as alm
from _test_helpers import tool_fn

get_issue = tool_fn("get_issue")
get_issues_by_query = tool_fn("get_issues_by_query")
create_issues = tool_fn("create_issues")
update_issues = tool_fn("update_issues")
list_issue_links = tool_fn("list_issue_links")
create_issue_links = tool_fn("create_issue_links")
list_issue_attachments = tool_fn("list_issue_attachments")
upload_issue_found_by_attachment = tool_fn("upload_issue_found_by_attachment")


# --- get_issue ---

def test_get_issue_builds_path_without_params(project, token_req):
    token_req.return_value = {"id": 3}
    out = get_issue(3)
    token_req.assert_called_once_with("GET", "/PROJ/issues/3")
    assert json.loads(out) == {"id": 3}


def test_get_issue_encodes_query_params(project, token_req):
    get_issue(3, fields=["Summary", "Status"], formatted_text=False, expand=["foundByRecords"])
    method, path = token_req.call_args.args
    assert method == "GET"
    assert path == (
        "/PROJ/issues/3?fields=Summary&fields=Status"
        "&formattedText=false&expand=foundByRecords"
    )


def test_get_issue_failure_returns_error_string(project, token_req):
    token_req.side_effect = RuntimeError("HTTP 404 Not Found")
    out = get_issue(999)
    assert out == "Error: HTTP 404 Not Found"


def test_get_issue_strips_expanded_attachments_content(project, token_req):
    """expand=["attachments"] embeds the same AttachmentContainer shape
    list_issue_attachments returns, including the dead `content` href —
    strip it here too, not just from the dedicated attachment tools."""
    token_req.return_value = {
        "id": 3,
        "attachments": {
            "attachmentsData": [{"id": 9, "encodedFileID": "abc", "content": "https://x/files/abc"}]
        },
    }
    out = get_issue(3, expand=["attachments"])
    assert json.loads(out) == {
        "id": 3,
        "attachments": {"attachmentsData": [{"id": 9, "encodedFileID": "abc"}]},
    }


def test_get_issue_strips_found_by_record_attachments_content(project, token_req):
    """Each found-by record (expand=["foundByRecords"]) carries its own
    nested attachments container, separate from the issue-level one — it
    needs the same content-href strip."""
    token_req.return_value = {
        "id": 3,
        "foundByRecords": {
            "foundByRecordsData": [
                {
                    "id": 1,
                    "attachments": {
                        "attachmentsData": [
                            {"id": 9, "encodedFileID": "abc", "content": "https://x/files/abc"}
                        ]
                    },
                }
            ]
        },
    }
    out = get_issue(3, expand=["foundByRecords"])
    assert json.loads(out)["foundByRecords"]["foundByRecordsData"][0]["attachments"] == {
        "attachmentsData": [{"id": 9, "encodedFileID": "abc"}]
    }


# --- search-body construction / routing ---

def test_default_args_send_empty_body(project, token_req):
    get_issues_by_query()
    method, path = token_req.call_args.args
    assert method == "POST"
    assert path == "/PROJ/issues/search"
    # page/per_page/formatted_text at their defaults must be omitted.
    assert token_req.call_args.kwargs["body"] == {}


def test_filters_and_raw_search_are_and_joined_with_parens(project, token_req):
    get_issues_by_query(filters={"Status": "Open"}, search="Description contains 'WysiCorp'")
    assert (
        token_req.call_args.kwargs["body"]["search"]
        == "\"Status\" = 'Open' and (Description contains 'WysiCorp')"
    )


def test_non_default_paging_and_fields_included(project, token_req):
    get_issues_by_query(
        filter_id="Assigned to Me", fields=["Summary", "Status"],
        page=2, per_page=50, formatted_text=False, expand=["foundByRecords"],
    )
    assert token_req.call_args.kwargs["body"] == {
        "filterID": "Assigned to Me",
        "fields": ["Summary", "Status"],
        "page": 2,
        "per_page": 50,
        "formattedText": False,
        "expand": ["foundByRecords"],
    }


def test_project_segment_is_percent_encoded(token_req):
    alm._current_project = "My Project"   # space must not break the URL path
    get_issues_by_query()
    _, path = token_req.call_args.args
    assert path == "/My%20Project/issues/search"


# --- shape contract (success / error) ---

def test_success_returns_indented_json(project, token_req):
    payload = {"issues": [{"id": 1}], "paging": {"page": 1}}
    token_req.return_value = payload
    out = get_issues_by_query()
    assert out == json.dumps(payload, indent=2)


def test_failure_returns_error_string_not_json(project, token_req):
    token_req.side_effect = RuntimeError("HTTP 400 Bad Request: nope")
    out = get_issues_by_query()
    assert out == "Error: HTTP 400 Bad Request: nope"
    assert not out.lstrip().startswith("{")


def test_connection_failure_surfaces_as_error_string(project, token_req):
    token_req.side_effect = urllib.error.URLError("Connection refused")
    out = get_issues_by_query()
    assert out.startswith("Error:")
    assert "Connection refused" in out


# --- found-by mirror field decoration (get_issue / get_issues_by_query only —
# create_issues/update_issues intentionally do not enforce this at write time,
# see the mirrored-field tests in their own sections below) ---

def test_get_issue_decorates_mirrored_fields_with_found_by_record_key(project, token_req):
    token_req.return_value = {
        "id": 9,
        "fields": [
            {"id": 2, "label": "Summary", "type": "string", "string": "S"},
            {"id": 54, "label": "Description", "type": "formattedString",
             "formattedString": {"text": "<p>x</p>", "isFormatted": True, "inlineImages": []}},
            {"id": 12, "label": "Version Found", "type": "string", "string": "1.0"},
        ],
    }
    out = get_issue(9)
    fields = json.loads(out)["fields"]
    assert "foundByRecordKey" not in fields[0]   # Summary isn't a mirror
    assert fields[1]["foundByRecordKey"] == "description"
    assert fields[2]["foundByRecordKey"] == "versionFound"


def test_get_issue_decoration_matches_by_id_even_if_label_is_renamed(project, token_req):
    """foundByRecordKey is keyed off field id — a fixed inherent-field id
    (0-199 are reserved for built-in fields, universally, not per-project row
    ids) — so the tag still applies even if a project has renamed the field's
    label away from its default (e.g. "Description" -> "Bug Summary")."""
    token_req.return_value = {
        "id": 9,
        "fields": [{"id": 54, "label": "Bug Summary", "type": "string", "string": "x"}],
    }
    out = get_issue(9)
    assert json.loads(out)["fields"][0]["foundByRecordKey"] == "description"


def test_get_issue_decoration_is_a_no_op_without_a_fields_key(project, token_req):
    token_req.return_value = {"id": 9}
    out = get_issue(9)
    assert json.loads(out) == {"id": 9}


def test_get_issues_by_query_decorates_mirrored_fields_on_each_issue(project, token_req):
    token_req.return_value = {
        "issues": [
            {"id": 1, "fields": [{"id": 54, "label": "Description", "type": "string", "string": "a"}]},
            {"id": 2, "fields": [{"id": 58, "label": "Steps to Reproduce", "type": "string", "string": "b"}]},
        ],
        "paging": {"page": 1},
    }
    out = get_issues_by_query()
    issues = json.loads(out)["issues"]
    assert issues[0]["fields"][0]["foundByRecordKey"] == "description"
    assert issues[1]["fields"][0]["foundByRecordKey"] == "steps"


def test_get_issues_by_query_strips_expanded_attachments_content(project, token_req):
    token_req.return_value = {
        "issues": [
            {
                "id": 1,
                "attachments": {
                    "attachmentsData": [{"id": 9, "encodedFileID": "abc", "content": "https://x/files/abc"}]
                },
            }
        ]
    }
    out = get_issues_by_query()
    assert json.loads(out)["issues"][0]["attachments"] == {
        "attachmentsData": [{"id": 9, "encodedFileID": "abc"}]
    }


def test_get_issues_by_query_strips_found_by_record_attachments_content(project, token_req):
    """Same found-by-record nested-attachments case as get_issue, but for
    each issue returned by the search."""
    token_req.return_value = {
        "issues": [
            {
                "id": 1,
                "foundByRecords": {
                    "foundByRecordsData": [
                        {
                            "id": 5,
                            "attachments": {
                                "attachmentsData": [
                                    {"id": 9, "encodedFileID": "abc", "content": "https://x/files/abc"}
                                ]
                            },
                        }
                    ]
                },
            }
        ]
    }
    out = get_issues_by_query()
    issue = json.loads(out)["issues"][0]
    assert issue["foundByRecords"]["foundByRecordsData"][0]["attachments"] == {
        "attachmentsData": [{"id": 9, "encodedFileID": "abc"}]
    }


# --- create_issues ---

def test_create_issues_wraps_envelopes(project, token_req_with_status):
    token_req_with_status.return_value = ({"issues": [{"id": 1}]}, 201)
    create_issues(
        fields=[
            {"label": "Summary", "string": "New issue from REST API"},
            {"label": "Type", "menuItem": {"label": "Question"}},
        ],
        found_by_records=[{"versionFound": "1.0"}],
        folders=[{"id": 2}],
    )
    method, path, body = token_req_with_status.call_args.args
    issues = body["issues"]
    assert (method, path) == ("POST", "/PROJ/issues")
    assert len(issues) == 1   # single-item arg is wrapped into a length-1 bulk list
    issue = issues[0]
    assert issue["fields"][0] == {"label": "Summary", "string": "New issue from REST API"}
    assert issue["foundByRecords"] == {"foundByRecordsData": [{"versionFound": "1.0"}]}
    assert issue["folders"] == {"foldersData": [{"id": 2}]}
    assert "attachments" not in issue  # no attachments arg — the create endpoint silently discards it anyway


def test_create_issues_omits_unset_optionals(project, token_req_with_status):
    create_issues(fields=[{"label": "Summary", "string": "S"}])
    assert token_req_with_status.call_args.args[2]["issues"][0] == {
        "fields": [{"label": "Summary", "string": "S"}]
    }


def test_create_issues_failure_returns_error_string(project, token_req_with_status):
    token_req_with_status.side_effect = RuntimeError("HTTP 400 Bad Request: bad field")
    out = create_issues(fields=[{"label": "Summary", "string": "S"}])
    assert out == "Error: HTTP 400 Bad Request: bad field"


def test_create_issues_found_by_records_passes_through_full_shape(project, token_req_with_status):
    """The full FoundByRecord shape documented in the docstring (every fixed key:
    dateFound, versionFound, description/steps/otherConfig as TextField,
    reproduced/testConfig as MenuItem, foundBy as User) passes through to
    foundByRecordsData unmodified — the tool does no per-key validation or
    transformation, just the envelope wrap."""
    record = {
        "dateFound": "2026-07-15",
        "versionFound": "1.0",
        "description": {"text": "Login fails after reset", "isFormatted": False},
        "reproduced": {"label": "Always"},
        "steps": {"text": "1. Reset password\n2. Log in", "isFormatted": False},
        "testConfig": {"label": "Windows 11 / Chrome"},
        "otherConfig": {"text": "Also fails on staging", "isFormatted": False},
        "foundBy": {"username": "jsmith"},
    }
    create_issues(fields=[{"label": "Summary", "string": "S"}], found_by_records=[record])
    issue = token_req_with_status.call_args.args[2]["issues"][0]
    assert issue["foundByRecords"] == {"foundByRecordsData": [record]}


def test_create_issues_mirrored_field_label_in_fields_is_not_special_cased(project, token_req_with_status):
    """A found-by-mirrored label (e.g. "Description") supplied via `fields` is
    NOT intercepted or rejected by the tool — it's forwarded as an ordinary
    field like any other. The silent no-op happens server-side, not in this
    tool, matching the documented "documentation-only fix, no runtime
    enforcement" design."""
    create_issues(fields=[
        {"label": "Summary", "string": "S"},
        {"label": "Description", "formattedString": {"isFormatted": False, "text": "Ignored by server"}},
    ])
    issue = token_req_with_status.call_args.args[2]["issues"][0]
    assert issue["fields"][1] == {
        "label": "Description",
        "formattedString": {"isFormatted": False, "text": "Ignored by server"},
    }


def test_create_issues_206_with_folders_adds_warning(project, token_req_with_status):
    """A create can succeed (item assigned an id) while its `folders` sub-object
    fails, since folder placement is not atomic with the create either — same
    footgun as the update_* tools, so this tool prepends the same `warning` key."""
    token_req_with_status.return_value = (
        {"issues": [{"id": 1}], "errors": [{"code": "E", "message": "bad folder", "statusCode": 404}]},
        206,
    )
    out = json.loads(create_issues(
        fields=[{"label": "Summary", "string": "S"}],
        folders=[{"id": 999999}],
    ))
    assert "WARNING" in out["warning"]
    assert "folders" in out["warning"]
    assert out["issues"] == [{"id": 1}]
    assert list(out.keys())[0] == "warning"  # prepended, not appended


def test_create_issues_206_without_folders_has_no_warning(project, token_req_with_status):
    token_req_with_status.return_value = (
        {"issues": [{"id": 1}], "errors": [{"code": "E", "message": "bad field", "statusCode": 400}]},
        206,
    )
    out = json.loads(create_issues(fields=[{"label": "Summary", "string": "S"}]))
    assert "warning" not in out


# --- update_issues ---

def test_update_issues_wraps_subobjects_per_item(project, token_req_with_status):
    token_req_with_status.return_value = ({"issues": [{"id": 10}]}, 200)
    out = update_issues([
        {
            "id": 10,
            "fields": [{"label": "Summary", "string": "S"}],
            "folders": [{"id": 2}],
            "foundByRecords": [{"id": 5, "versionFound": "1.0"}, {"versionFound": "2.0"}],
        }
    ])
    method, path, body = token_req_with_status.call_args.args
    item = body["issues"][0]
    assert (method, path) == ("PUT", "/PROJ/issues")
    assert item["id"] == 10
    assert item["fields"] == [{"label": "Summary", "string": "S"}]
    assert item["folders"] == {"foldersData": [{"id": 2}]}
    assert item["foundByRecords"] == {
        "foundByRecordsData": [{"id": 5, "versionFound": "1.0"}, {"versionFound": "2.0"}]
    }
    assert "warning" not in json.loads(out)  # plain 200, no partial-success signal


def test_update_issues_206_with_folders_adds_warning(project, token_req_with_status):
    """On a 206 partial-success PUT that touched `folders`, the REST API's
    folders replace has already removed existing placements and applies
    every valid entry in the list regardless of an invalid entry elsewhere
    in it — so this tool prepends a `warning` key rather than passing the
    response through silently."""
    token_req_with_status.return_value = (
        {"issues": [{"id": 10}], "errors": [{"code": "E", "message": "bad folder", "statusCode": 400}]},
        206,
    )
    out = json.loads(update_issues([{"id": 10, "folders": [{"id": 999999}]}]))
    assert "WARNING" in out["warning"]
    assert "folders" in out["warning"]
    assert out["issues"] == [{"id": 10}]
    assert list(out.keys())[0] == "warning"  # prepended, not appended


def test_update_issues_206_without_folders_has_no_warning(project, token_req_with_status):
    """The partial-success warning is scoped to the folders footgun — a 206
    on a request that never touched `folders` shouldn't claim a folders risk
    that isn't there."""
    token_req_with_status.return_value = (
        {"issues": [{"id": 10}], "errors": [{"code": "E", "message": "bad field", "statusCode": 400}]},
        206,
    )
    out = json.loads(update_issues([{"id": 10, "fields": []}]))
    assert "warning" not in out


def test_update_issues_mirrored_field_label_in_fields_is_not_special_cased(project, token_req_with_status):
    """Symmetric with the create_issues counterpart: update_issues doesn't
    intercept a found-by-mirrored label in `fields` either — it's forwarded
    as-is. Confirmed live that the server then silently no-ops the write
    (success response, value unchanged), which is exactly why
    test_update_issues_found_by_records_passes_through_full_shape's
    `foundByRecords` path is the one that actually persists a change."""
    update_issues([{
        "id": 8,
        "fields": [{"label": "Description",
                    "formattedString": {"isFormatted": False, "text": "Ignored by server"}}],
    }])
    item = token_req_with_status.call_args.args[2]["issues"][0]
    assert item["fields"] == [
        {"label": "Description", "formattedString": {"isFormatted": False, "text": "Ignored by server"}}
    ]


def test_update_issues_found_by_records_passes_through_full_shape(project, token_req_with_status):
    """Same full-shape pass-through guarantee as create_issues, but on update —
    including the record `id` needed to target an existing found-by record
    rather than add a new one. Confirmed live: updating `description`/`steps`
    this way actually persists, unlike the same fields sent via `fields`
    (see test_update_issues_mirrored_field_label_in_fields_is_not_special_cased
    for the tool-level counterpart of that behavior)."""
    record = {
        "id": 8,
        "dateFound": "2026-07-15",
        "versionFound": "1.0",
        "description": {"text": "Updated description", "isFormatted": False},
        "reproduced": {"label": "Always"},
        "steps": {"text": "Updated steps", "isFormatted": False},
        "testConfig": {"label": "Windows 11 / Chrome"},
        "otherConfig": {"text": "Also fails on staging", "isFormatted": False},
        "foundBy": {"username": "jsmith"},
    }
    update_issues([{"id": 8, "foundByRecords": [record]}])
    item = token_req_with_status.call_args.args[2]["issues"][0]
    assert item["foundByRecords"] == {"foundByRecordsData": [record]}


def test_update_issues_rejects_item_missing_id(project, token_req_with_status):
    out = update_issues([{"fields": []}])
    assert out == "Error: issues[0] missing the required 'id' key."
    token_req_with_status.assert_not_called()  # validation fails before any HTTP call


def test_update_issues_rejects_not_updatable_keys(project, token_req_with_status):
    """attachments/events/links can't be changed via PUT — explicit, targeted message."""
    out = update_issues([{"id": 10, "attachments": [{"id": 1}]}])
    assert out == (
        "Error: issues[0] cannot include ['attachments'] in an update: "
        "attachments cannot be updated with PUT. Add them with upload_issue_found_by_attachment."
    )
    token_req_with_status.assert_not_called()


def test_update_issues_rejects_not_updatable_links_key(project, token_req_with_status):
    """links gets its own targeted message, not the attachments/events wording."""
    out = update_issues([{"id": 10, "links": [{"id": 1}]}])
    assert out == (
        "Error: issues[0] cannot include ['links'] in an update: "
        "links do not have an editing path. Add them with create_issue_links "
        "instead of update_issues."
    )
    token_req_with_status.assert_not_called()


def test_update_issues_rejects_multiple_not_updatable_keys(project, token_req_with_status):
    """Multiple blocked keys join their individual reasons with a semicolon."""
    out = update_issues([{"id": 10, "attachments": [{"id": 1}], "links": [{"id": 1}]}])
    assert out == (
        "Error: issues[0] cannot include ['attachments', 'links'] in an update: "
        "attachments cannot be updated with PUT. Add them with upload_issue_found_by_attachment.; "
        "links do not have an editing path. Add them with create_issue_links "
        "instead of update_issues."
    )
    token_req_with_status.assert_not_called()


def test_update_issues_rejects_unknown_keys(project, token_req_with_status):
    out = update_issues([{"id": 10, "bogus": 1}])
    assert out == "Error: issues[0] has unknown keys: ['bogus']"
    token_req_with_status.assert_not_called()


# --- list/create_issue_links ---

def test_list_issue_links_path(project, token_req):
    token_req.return_value = {"linksData": []}
    list_issue_links(7)
    token_req.assert_called_once_with("GET", "/PROJ/issues/7/links")


def test_create_issue_links_wraps_linksdata(project, token_req):
    links = [{"linkDefinition": {"id": 3}, "type": "peers",
              "peers": [{"itemID": 9, "itemType": "requirements"}]}]
    create_issue_links(7, links)
    method, path = token_req.call_args.args
    assert (method, path) == ("POST", "/PROJ/issues/7/links")
    assert token_req.call_args.kwargs["body"] == {"linksData": links}


# --- list_issue_attachments / upload_issue_found_by_attachment ---
# Issues have no item-level attachment resource: attachments live on a found-by
# record, reached via a distinct path from the aggregating GET.

def test_list_issue_attachments_path(project, token_req):
    token_req.return_value = {
        "attachmentsData": [{"id": 9, "encodedFileID": "abc", "content": "https://x/files/abc"}]
    }
    out = list_issue_attachments(7)
    token_req.assert_called_once_with("GET", "/PROJ/issues/7/attachments")
    assert json.loads(out) == {"attachmentsData": [{"id": 9, "encodedFileID": "abc"}]}


def test_upload_issue_found_by_attachment_delegates_to_multipart_helper(project):
    with patch.object(
        alm,
        "_upload_attachment_request",
        return_value={"attachmentsData": [{"id": 9, "encodedFileID": "abc", "content": "https://x/files/abc"}]},
    ) as up:
        out = upload_issue_found_by_attachment(7, 3, "C:/tmp/screenshot.png")
    up.assert_called_once_with("/PROJ/issues/7/foundByRecords/3/attachments", "C:/tmp/screenshot.png")
    assert json.loads(out) == {"attachmentsData": [{"id": 9, "encodedFileID": "abc"}]}


def test_upload_issue_found_by_attachment_missing_file_returns_error(project, non_token_req, tmp_path, monkeypatch):
    monkeypatch.setenv("PERFORCE_ALM_MCP_UPLOAD_DIR", str(tmp_path))
    out = upload_issue_found_by_attachment(7, 3, str(tmp_path / "exist_xyz.bin"))
    assert out.startswith("Error:")
    assert "exist_xyz.bin" in out
    non_token_req.assert_not_called()  # the missing-file check must fire before any token/network call
