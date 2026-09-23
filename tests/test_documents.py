"""Document tools: get/create/update, search, trees, snapshots, links, and attachments.

Highlights: the ``snapshot`` query param on get_document/get_document_tree, the
``expand=nodes`` + ``recursive=true`` full-tree fetch, the dual-endpoint
branching in add_document_tree_nodes (nodesData vs childNodesData), and the
single-snapshot wrapping of create_document_snapshot.
"""
import json
from unittest.mock import patch

import perforce_alm_mcp as alm
from _test_helpers import tool_fn

get_document = tool_fn("get_document")
get_documents_by_query = tool_fn("get_documents_by_query")
create_documents = tool_fn("create_documents")
update_documents = tool_fn("update_documents")
get_document_tree = tool_fn("get_document_tree")
add_document_tree_nodes = tool_fn("add_document_tree_nodes")
list_document_snapshots = tool_fn("list_document_snapshots")
create_document_snapshot = tool_fn("create_document_snapshot")
list_document_links = tool_fn("list_document_links")
create_document_links = tool_fn("create_document_links")
list_document_attachments = tool_fn("list_document_attachments")
upload_document_attachment = tool_fn("upload_document_attachment")


# --- get_document ---

def test_get_document_path_without_params(project, token_req):
    token_req.return_value = {"id": 4}
    out = get_document(4)
    token_req.assert_called_once_with("GET", "/PROJ/documents/4")
    assert json.loads(out) == {"id": 4}


def test_get_document_encodes_query_params_including_snapshot(project, token_req):
    get_document(4, fields=["Summary"], formatted_text=False, expand=["snapshots"], snapshot=3)
    method, path = token_req.call_args.args
    assert method == "GET"
    assert path == "/PROJ/documents/4?fields=Summary&formattedText=false&expand=snapshots&snapshot=3"


def test_get_document_strips_expanded_attachments_content(project, token_req):
    """expand=["attachments"] embeds the same AttachmentContainer shape
    list_document_attachments returns, including the dead `content` href —
    strip it here too, not just from the dedicated attachment tools."""
    token_req.return_value = {
        "id": 4,
        "attachments": {
            "attachmentsData": [{"id": 9, "encodedFileID": "abc", "content": "https://x/files/abc"}]
        },
    }
    out = get_document(4, expand=["attachments"])
    assert json.loads(out) == {
        "id": 4,
        "attachments": {"attachmentsData": [{"id": 9, "encodedFileID": "abc"}]},
    }


# --- get_documents_by_query ---

def test_query_routes_to_documents_search(project, token_req):
    get_documents_by_query(filters={"Status": "Approved"})
    method, path = token_req.call_args.args
    assert (method, path) == ("POST", "/PROJ/documents/search")
    assert token_req.call_args.kwargs["body"]["search"] == "\"Status\" = 'Approved'"


def test_get_documents_by_query_strips_expanded_attachments_content(project, token_req):
    token_req.return_value = {
        "documents": [
            {
                "id": 4,
                "attachments": {
                    "attachmentsData": [{"id": 9, "encodedFileID": "abc", "content": "https://x/files/abc"}]
                },
            }
        ]
    }
    out = get_documents_by_query()
    assert json.loads(out)["documents"][0]["attachments"] == {
        "attachmentsData": [{"id": 9, "encodedFileID": "abc"}]
    }


# --- create_documents ---

def test_create_documents_wraps_envelopes(project, token_req_with_status):
    token_req_with_status.return_value = ({"documents": [{"id": 9}]}, 201)
    create_documents(
        fields=[{"id": 1, "type": "string", "string": "Doc title"}],
        folders=[{"id": 135}],
    )
    method, path, body = token_req_with_status.call_args.args
    docs = body["documents"]
    assert (method, path) == ("POST", "/PROJ/documents")
    assert len(docs) == 1
    doc = docs[0]
    assert doc["fields"] == [{"id": 1, "type": "string", "string": "Doc title"}]
    assert doc["folders"] == {"foldersData": [{"id": 135}]}


def test_create_documents_omits_unset_subobjects(project, token_req_with_status):
    create_documents(fields=[{"id": 1, "type": "boolean", "boolean": True}])
    assert token_req_with_status.call_args.args[2]["documents"][0] == {
        "fields": [{"id": 1, "type": "boolean", "boolean": True}]
    }


def test_create_documents_206_with_folders_adds_warning(project, token_req_with_status):
    """A create can succeed (item assigned an id) while its `folders` sub-object
    fails, since folder placement is not atomic with the create either — same
    footgun as the update_* tools, so this tool prepends the same `warning` key."""
    token_req_with_status.return_value = (
        {"documents": [{"id": 9}], "errors": [{"code": "E", "message": "bad folder", "statusCode": 404}]},
        206,
    )
    out = json.loads(create_documents(
        fields=[{"id": 1, "type": "string", "string": "Doc title"}],
        folders=[{"id": 999999}],
    ))
    assert "WARNING" in out["warning"]
    assert "folders" in out["warning"]
    assert out["documents"] == [{"id": 9}]
    assert list(out.keys())[0] == "warning"  # prepended, not appended


def test_create_documents_206_without_folders_has_no_warning(project, token_req_with_status):
    token_req_with_status.return_value = (
        {"documents": [{"id": 9}], "errors": [{"code": "E", "message": "bad field", "statusCode": 400}]},
        206,
    )
    out = json.loads(create_documents(fields=[{"id": 1, "type": "boolean", "boolean": True}]))
    assert "warning" not in out


# --- update_documents ---

def test_update_documents_wraps_subobjects_per_item(project, token_req_with_status):
    token_req_with_status.return_value = ({"documents": [{"id": 9}]}, 200)
    out = update_documents([
        {
            "id": 9,
            "fields": [{"id": 1, "type": "string", "string": "New title"}],
            "folders": [{"id": 135}],
        }
    ])
    method, path, body = token_req_with_status.call_args.args
    item = body["documents"][0]
    assert (method, path) == ("PUT", "/PROJ/documents")
    assert item["id"] == 9
    assert item["fields"] == [{"id": 1, "type": "string", "string": "New title"}]
    assert item["folders"] == {"foldersData": [{"id": 135}]}
    assert "warning" not in json.loads(out)  # plain 200, no partial-success signal


def test_update_documents_206_with_folders_adds_warning(project, token_req_with_status):
    """On a 206 partial-success PUT that touched `folders`, the REST API's
    folders replace has already removed existing placements and applies
    every valid entry in the list regardless of an invalid entry elsewhere
    in it — so this tool prepends a `warning` key rather than passing the
    response through silently."""
    token_req_with_status.return_value = (
        {"documents": [{"id": 9}], "errors": [{"code": "E", "message": "bad folder", "statusCode": 400}]},
        206,
    )
    out = json.loads(update_documents([{"id": 9, "folders": [{"id": 999999}]}]))
    assert "WARNING" in out["warning"]
    assert "folders" in out["warning"]
    assert out["documents"] == [{"id": 9}]
    assert list(out.keys())[0] == "warning"  # prepended, not appended


def test_update_documents_206_without_folders_has_no_warning(project, token_req_with_status):
    """The partial-success warning is scoped to the folders footgun — a 206
    on a request that never touched `folders` shouldn't claim a folders risk
    that isn't there."""
    token_req_with_status.return_value = (
        {"documents": [{"id": 9}], "errors": [{"code": "E", "message": "bad field", "statusCode": 400}]},
        206,
    )
    out = json.loads(update_documents([{"id": 9, "fields": []}]))
    assert "warning" not in out


def test_update_documents_rejects_attachments_key(project, token_req_with_status):
    """The REST update endpoint silently discards an attachments payload
    (verified empirically against a live server) — rejected here so the
    caller gets a targeted error instead of a false sense that the change
    took effect."""
    out = update_documents([{"id": 9, "attachments": [{"id": 42}]}])
    assert out == (
        "Error: documents[0] cannot include ['attachments'] in an update: "
        "attachments cannot be updated with PUT. Add them with upload_document_attachment."
    )
    token_req_with_status.assert_not_called()


def test_update_documents_rejects_item_missing_id(project, token_req_with_status):
    out = update_documents([{"fields": []}])
    assert out == "Error: documents[0] missing the required 'id' key."
    token_req_with_status.assert_not_called()  # validation fails before any HTTP call


def test_update_documents_rejects_inline_links(project, token_req_with_status):
    """Inline link editing is intentionally not supported on update_documents
    (a PUT replaces the whole link set wholesale — too easy to clobber). Links
    are managed via the dedicated *_document_links tools."""
    out = update_documents([{"id": 9, "links": []}])
    assert out == (
        "Error: documents[0] cannot include ['links'] in an update: "
        "links do not have an editing path. Add them with create_document_links "
        "instead of update_documents."
    )
    token_req_with_status.assert_not_called()


def test_update_documents_rejects_events_key(project, token_req_with_status):
    out = update_documents([{"id": 9, "events": []}])
    assert out == (
        "Error: documents[0] cannot include ['events'] in an update: "
        "events cannot be updated with PUT."
    )
    token_req_with_status.assert_not_called()


def test_update_documents_rejects_snapshots_key(project, token_req_with_status):
    out = update_documents([{"id": 9, "snapshots": []}])
    assert out == (
        "Error: documents[0] cannot include ['snapshots'] in an update: "
        "snapshots cannot be updated with PUT. Add them with create_document_snapshot."
    )
    token_req_with_status.assert_not_called()


def test_update_documents_rejects_unknown_keys(project, token_req_with_status):
    out = update_documents([{"id": 1, "bogus": 1}])
    assert out == "Error: documents[0] has unknown keys: ['bogus']"
    token_req_with_status.assert_not_called()


# --- get_document_tree ---

def test_get_document_tree_no_params(project, token_req):
    token_req.return_value = {"id": 4, "name": "Spec"}
    get_document_tree(4)
    token_req.assert_called_once_with("GET", "/PROJ/documentTrees/4")


def test_get_document_tree_full_tree_query(project, token_req):
    """Both expand=nodes AND recursive=true are needed for the full tree."""
    get_document_tree(4, expand=["nodes"], recursive=True, snapshot=2)
    method, path = token_req.call_args.args
    assert method == "GET"
    assert path == "/PROJ/documentTrees/4?expand=nodes&recursive=true&snapshot=2"


# --- add_document_tree_nodes: dual-endpoint branching ---

def test_add_nodes_top_level_uses_nodesdata(project, token_req):
    token_req.return_value = {"nodesData": []}
    add_document_tree_nodes(4, [10, 11])   # parent_node_id defaults to 0 -> top level
    method, path = token_req.call_args.args
    assert (method, path) == ("POST", "/PROJ/documentTrees/4/nodes")
    assert token_req.call_args.kwargs["body"] == {
        "nodesData": [{"requirementID": 10}, {"requirementID": 11}]
    }


def test_add_nodes_under_parent_uses_childnodesdata(project, token_req):
    token_req.return_value = {"childNodesData": []}
    add_document_tree_nodes(4, [10], parent_node_id=99)
    method, path = token_req.call_args.args
    assert (method, path) == ("POST", "/PROJ/documentTrees/4/nodes/99/childNodes")
    assert token_req.call_args.kwargs["body"] == {
        "childNodesData": [{"requirementID": 10}]
    }


# --- list/create_document_snapshot ---

def test_list_document_snapshots_path(project, token_req):
    token_req.return_value = {"snapshotsData": []}
    list_document_snapshots(4)
    token_req.assert_called_once_with("GET", "/PROJ/documents/4/snapshots")


def test_create_document_snapshot_with_comment(project, token_req):
    token_req.return_value = {"snapshotsData": [{"snapshot": 1}]}
    create_document_snapshot(4, "First revision", comment="Ready for review")
    method, path = token_req.call_args.args
    assert (method, path) == ("POST", "/PROJ/documents/4/snapshots")
    assert token_req.call_args.kwargs["body"] == {
        "snapshotsData": [{"label": "First revision", "comment": "Ready for review"}]
    }


def test_create_document_snapshot_omits_blank_comment(project, token_req):
    create_document_snapshot(4, "First revision")
    assert token_req.call_args.kwargs["body"] == {
        "snapshotsData": [{"label": "First revision"}]
    }


# --- list/create_document_links ---

def test_list_document_links_path(project, token_req):
    token_req.return_value = {"linksData": []}
    list_document_links(4)
    token_req.assert_called_once_with("GET", "/PROJ/documents/4/links")


def test_create_document_links_wraps_linksdata(project, token_req):
    links = [{"linkDefinition": {"id": 3}, "type": "peers",
              "peers": [{"itemID": 9, "itemType": "requirements"}]}]
    create_document_links(4, links)
    method, path = token_req.call_args.args
    assert (method, path) == ("POST", "/PROJ/documents/4/links")
    assert token_req.call_args.kwargs["body"] == {"linksData": links}


# --- list/upload document attachments ---

def test_list_document_attachments_path(project, token_req):
    token_req.return_value = {
        "attachmentsData": [{"id": 3, "encodedFileID": "abc", "content": "https://x/files/abc"}]
    }
    out = list_document_attachments(4)
    token_req.assert_called_once_with("GET", "/PROJ/documents/4/attachments")
    assert json.loads(out) == {"attachmentsData": [{"id": 3, "encodedFileID": "abc"}]}


def test_upload_document_attachment_delegates_to_multipart_helper(project):
    with patch.object(
        alm,
        "_upload_attachment_request",
        return_value={"attachmentsData": [{"id": 3, "encodedFileID": "abc", "content": "https://x/files/abc"}]},
    ) as up:
        out = upload_document_attachment(4, "C:/tmp/notes.txt")
    up.assert_called_once_with("/PROJ/documents/4/attachments", "C:/tmp/notes.txt")
    assert json.loads(out) == {"attachmentsData": [{"id": 3, "encodedFileID": "abc"}]}


def test_upload_document_attachment_missing_file_returns_error(project, non_token_req, tmp_path, monkeypatch):
    monkeypatch.setenv("PERFORCE_ALM_MCP_UPLOAD_DIR", str(tmp_path))
    out = upload_document_attachment(4, str(tmp_path / "exist_xyz.bin"))
    assert out.startswith("Error:")
    assert "exist_xyz.bin" in out
    non_token_req.assert_not_called()  # the missing-file check must fire before any token/network call
