"""Tests that exercise real FastMCP argument validation (not tool_fn's raw
function), because the bug/fix live in the pydantic validation layer FastMCP
wraps around each tool, not in the tool bodies themselves."""
import asyncio

import pytest
from pydantic import ValidationError

import perforce_alm_mcp as alm


def _call_tool(tool_name, **kwargs):
    async def _run():
        tool = await alm.mcp.get_tool(tool_name)
        return await tool.run(kwargs)
    return asyncio.run(_run())


# --- _coerce_json itself ---

def test_coerce_json_parses_valid_json_string():
    assert alm._coerce_json('{"id": 1}') == {"id": 1}
    assert alm._coerce_json('["a", "b"]') == ["a", "b"]


def test_coerce_json_leaves_malformed_json_string_unchanged():
    # Not silently mangled into {} or None — left as-is so the surrounding
    # pydantic validation still raises a clear, honest error.
    assert alm._coerce_json("not-json-at-all") == "not-json-at-all"


def test_coerce_json_leaves_non_string_values_unchanged():
    assert alm._coerce_json({"id": 1}) == {"id": 1}
    assert alm._coerce_json(["a", "b"]) == ["a", "b"]
    assert alm._coerce_json(None) is None


# --- fallback behavior through the real validation layer ---

def test_create_requirements_malformed_json_string_still_raises_clearly(project, token_req_with_status):
    with pytest.raises(ValidationError, match="requirement_type"):
        _call_tool(
            "create_requirements",
            fields=[{"id": 1, "type": "boolean", "boolean": True}],
            requirement_type="not-json-at-all",
        )
    token_req_with_status.assert_not_called()  # must fail before any HTTP call


def test_get_requirement_accepts_a_real_list_unchanged(project, token_req):
    """A well-behaved client that already sends a real array/object (not a
    JSON string) must be unaffected by the BeforeValidator."""
    _call_tool("get_requirement", item_id=3, fields=["Summary"], expand=["links"])
    _, path = token_req.call_args.args
    assert path == "/PROJ/requirements/3?fields=Summary&expand=links"


def test_create_requirements_coerces_stringified_requirement_type_and_folders(project, token_req_with_status):
    _call_tool(
        "create_requirements",
        fields=[{"id": 1, "type": "boolean", "boolean": True}],
        requirement_type='{"id": 7}',
        folders='[{"id": 2}]',
    )
    req = token_req_with_status.call_args.args[2]["requirements"][0]
    assert req["requirementType"] == {"id": 7}
    assert req["folders"] == {"foldersData": [{"id": 2}]}


def test_get_requirement_coerces_stringified_fields_and_expand(project, token_req):
    _call_tool("get_requirement", item_id=3, fields='["Summary"]', expand='["links"]')
    _, path = token_req.call_args.args
    assert path == "/PROJ/requirements/3?fields=Summary&expand=links"


def test_get_requirements_by_query_coerces_stringified_filters_fields_expand(project, token_req):
    _call_tool(
        "get_requirements_by_query",
        filters='{"Product": "WysiCorp"}',
        fields='["Summary"]',
        expand='["links"]',
    )
    body = token_req.call_args.kwargs["body"]
    assert body["search"] == "\"Product\" = 'WysiCorp'"
    assert body["fields"] == ["Summary"]
    assert body["expand"] == ["links"]


def test_get_issue_coerces_stringified_fields_and_expand(project, token_req):
    _call_tool("get_issue", item_id=3, fields='["Summary"]', expand='["links"]')
    _, path = token_req.call_args.args
    assert path == "/PROJ/issues/3?fields=Summary&expand=links"


def test_get_issues_by_query_coerces_stringified_filters_fields_expand(project, token_req):
    _call_tool(
        "get_issues_by_query",
        filters='{"Status": "Open"}',
        fields='["Summary"]',
        expand='["links"]',
    )
    body = token_req.call_args.kwargs["body"]
    assert body["search"] == "\"Status\" = 'Open'"
    assert body["fields"] == ["Summary"]
    assert body["expand"] == ["links"]


def test_create_issues_coerces_stringified_found_by_records_and_folders(project, token_req_with_status):
    _call_tool(
        "create_issues",
        fields=[{"id": 1, "type": "boolean", "boolean": True}],
        found_by_records='[{"description": {"text": "x", "isFormatted": false}}]',
        folders='[{"id": 2}]',
    )
    issue = token_req_with_status.call_args.args[2]["issues"][0]
    assert issue["foundByRecords"] == {
        "foundByRecordsData": [{"description": {"text": "x", "isFormatted": False}}]
    }
    assert issue["folders"] == {"foldersData": [{"id": 2}]}


def test_get_testcases_by_query_coerces_stringified_filters_fields_expand(project, token_req):
    _call_tool(
        "get_testcases_by_query",
        filters='{"Product": "WysiCorp"}',
        fields='["Summary"]',
        expand='["links"]',
    )
    body = token_req.call_args.kwargs["body"]
    assert body["search"] == "\"Product\" = 'WysiCorp'"
    assert body["fields"] == ["Summary"]
    assert body["expand"] == ["links"]


def test_get_testcase_coerces_stringified_fields_and_expand(project, token_req):
    _call_tool("get_testcase", item_id=3, fields='["Summary"]', expand='["links"]')
    _, path = token_req.call_args.args
    assert path == "/PROJ/testCases/3?fields=Summary&expand=links"


def test_create_testcases_coerces_stringified_folders_scripts_variants_steps(project, token_req_with_status):
    _call_tool(
        "create_testcases",
        fields=[{"id": 1, "type": "boolean", "boolean": True}],
        folders='[{"id": 2}]',
        scripts='[{"id": 95, "referenceType": "attachment"}]',
        variants='{"included": [], "excluded": []}',
        steps='{"type": "detailed", "detailed": []}',
    )
    testcase = token_req_with_status.call_args.args[2]["testCases"][0]
    assert testcase["folders"] == {"foldersData": [{"id": 2}]}
    assert testcase["scripts"] == {"scriptsData": [{"id": 95, "referenceType": "attachment"}]}
    assert testcase["variants"] == {"variantsData": {"included": [], "excluded": []}}
    assert testcase["steps"] == {"stepsData": {"type": "detailed", "detailed": []}}


def test_get_document_coerces_stringified_fields_and_expand(project, token_req):
    _call_tool("get_document", item_id=3, fields='["Summary"]', expand='["links"]')
    _, path = token_req.call_args.args
    assert path == "/PROJ/documents/3?fields=Summary&expand=links"


def test_get_documents_by_query_coerces_stringified_filters_fields_expand(project, token_req):
    _call_tool(
        "get_documents_by_query",
        filters='{"Status": "Approved"}',
        fields='["Summary"]',
        expand='["links"]',
    )
    body = token_req.call_args.kwargs["body"]
    assert body["search"] == "\"Status\" = 'Approved'"
    assert body["fields"] == ["Summary"]
    assert body["expand"] == ["links"]


def test_create_documents_coerces_stringified_folders(project, token_req_with_status):
    _call_tool(
        "create_documents",
        fields=[{"id": 1, "type": "boolean", "boolean": True}],
        folders='[{"id": 2}]',
    )
    doc = token_req_with_status.call_args.args[2]["documents"][0]
    assert doc["folders"] == {"foldersData": [{"id": 2}]}


def test_get_document_tree_coerces_stringified_expand(project, token_req):
    _call_tool("get_document_tree", item_id=3, expand='["nodes"]')
    _, path = token_req.call_args.args
    assert path == "/PROJ/documentTrees/3?expand=nodes"


def test_list_automation_suites_coerces_stringified_expand(project, token_req):
    _call_tool("list_automation_suites", expand='["testCases"]')
    _, path = token_req.call_args.args
    assert path == "/PROJ/automationSuites?expand=testCases"


def test_get_automation_suite_coerces_stringified_expand(project, token_req):
    _call_tool("get_automation_suite", automation_suite_id=5, expand='["testCases"]')
    _, path = token_req.call_args.args
    assert path == "/PROJ/automationSuites/5?expand=testCases"


def test_create_automation_suites_coerces_stringified_owners_run_configuration_testcases(project, token_req):
    _call_tool(
        "create_automation_suites",
        name="Suite",
        owners='[{"username": "jsmith"}]',
        run_configuration='{"type": "jenkins"}',
        testcases='[{"id": 10}]',
    )
    suite = token_req.call_args.kwargs["body"]["automationSuitesData"][0]
    assert suite["owners"] == [{"username": "jsmith"}]
    assert suite["runConfiguration"] == {"type": "jenkins"}
    assert suite["testCases"] == {"testCasesData": [{"id": 10}]}


def test_update_automation_suite_coerces_stringified_owners_run_configuration_testcases(project, token_req):
    _call_tool(
        "update_automation_suite",
        automation_suite_id=5,
        owners='[{"username": "jsmith"}]',
        run_configuration='{"type": "jenkins"}',
        testcases='[{"id": 10}]',
    )
    body = token_req.call_args.kwargs["body"]
    assert body["owners"] == [{"username": "jsmith"}]
    assert body["runConfiguration"] == {"type": "jenkins"}
    assert body["testCases"] == {"testCasesData": [{"id": 10}]}


def test_run_automation_suite_coerces_stringified_test_run_set_and_build_parameters(project, token_req):
    _call_tool(
        "run_automation_suite",
        automation_suite_id=5,
        test_run_set='{"id": 1}',
        build_parameters='[{"name": "X", "type": "text", "text": "Y"}]',
    )
    body = token_req.call_args.kwargs["body"]
    assert body["testRunSet"] == {"id": 1}
    assert body["jenkins"]["buildParameters"] == [{"name": "X", "type": "text", "text": "Y"}]


def test_list_automation_suite_builds_coerces_stringified_status_test_run_sets_users(project, token_req):
    _call_tool(
        "list_automation_suite_builds",
        automation_suite_id=5,
        status='["finished"]',
        test_run_sets='[1, "Regression"]',
        users='["jsmith", 2]',
    )
    _, path = token_req.call_args.args
    assert "status=finished" in path
    assert "test_run_sets=1%2CRegression" in path
    assert "users=jsmith%2C2" in path


def test_submit_automation_build_coerces_stringified_results_test_run_set_properties(project, token_req):
    _call_tool(
        "submit_automation_build",
        automation_suite_id=5,
        number="1",
        results='[{"name": "T1", "uniqueName": "u1", "status": {"label": "passed"}}]',
        test_run_set='{"id": 1}',
        properties='[{"name": "k", "value": "v"}]',
    )
    body = token_req.call_args.kwargs["body"]
    assert body["results"] == [{"name": "T1", "uniqueName": "u1", "status": {"label": "passed"}}]
    assert body["testRunSet"] == {"id": 1}
    assert body["properties"] == [{"name": "k", "value": "v"}]


def test_list_automation_build_results_coerces_stringified_expand(project, token_req):
    _call_tool("list_automation_build_results", automation_suite_id=5, build_id=9, expand='["links"]')
    _, path = token_req.call_args.args
    assert path.endswith("?expand=links")


def test_get_automation_build_result_coerces_stringified_expand(project, token_req):
    _call_tool(
        "get_automation_build_result", automation_suite_id=5, build_id=9, result_id=1, expand='["links"]'
    )
    _, path = token_req.call_args.args
    assert path.endswith("?expand=links")
