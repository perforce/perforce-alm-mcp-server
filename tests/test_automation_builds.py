"""Automation build-result tools: submit_automation_build,
list_automation_build_results, get_automation_build_result,
associate_automation_results.

Highlights: submit_automation_build body construction (required ``number`` plus
optional results/metadata, omitting unset), the nested results paths, and the
synthesized confirmation payload on associate_automation_results.
"""
import json
from urllib.parse import urlparse, parse_qs

import perforce_alm_mcp as alm
from _test_helpers import tool_fn

submit_automation_build = tool_fn("submit_automation_build")
list_automation_build_results = tool_fn("list_automation_build_results")
get_automation_build_result = tool_fn("get_automation_build_result")
associate_automation_results = tool_fn("associate_automation_results")


# --- submit_automation_build ---

def test_submit_build_minimal_sends_only_number(project, token_req):
    token_req.return_value = {"buildsData": [{"id": 9}]}
    submit_automation_build(5, "167")
    method, path = token_req.call_args.args
    assert (method, path) == ("POST", "/PROJ/automationSuites/5/submitBuild")
    assert token_req.call_args.kwargs["body"] == {"number": "167"}


def test_submit_build_full_body(project, token_req):
    results = [{"name": "t1", "uniqueName": "suite.t1", "status": {"label": "passed"}}]
    submit_automation_build(
        5, "167",
        results=results,
        description="nightly",
        branch="main",
        start_date="2026-05-20T00:00:00Z",
        duration=1234,
        test_run_set={"id": 2},
        external_url="https://ci/job/167",
        properties=[{"name": "commit", "value": "abc"}],
    )
    body = token_req.call_args.kwargs["body"]
    assert body["number"] == "167"
    assert body["results"] == results
    assert body["description"] == "nightly"
    assert body["branch"] == "main"
    assert body["startDate"] == "2026-05-20T00:00:00Z"
    assert body["duration"] == 1234
    assert body["testRunSet"] == {"id": 2}
    assert body["externalURL"] == "https://ci/job/167"
    assert body["properties"] == [{"name": "commit", "value": "abc"}]


# --- list_automation_build_results ---

def test_list_build_results_path(project, token_req):
    token_req.return_value = {"resultsData": []}
    list_automation_build_results(5, 9)
    token_req.assert_called_once_with(
        "GET", "/PROJ/automationSuites/5/builds/9/results"
    )


def test_list_build_results_with_expand(project, token_req):
    list_automation_build_results(5, 9, expand=["testCases", "links"])
    _, path = token_req.call_args.args
    q = parse_qs(urlparse(path).query)
    assert path.startswith("/PROJ/automationSuites/5/builds/9/results?")
    assert q["expand"] == ["testCases", "links"]


# --- get_automation_build_result ---

def test_get_build_result_path(project, token_req):
    token_req.return_value = {"id": 42}
    get_automation_build_result(5, 9, 42)
    token_req.assert_called_once_with(
        "GET", "/PROJ/automationSuites/5/builds/9/results/42"
    )


def test_get_build_result_with_expand(project, token_req):
    get_automation_build_result(5, 9, 42, expand=["testCases"])
    _, path = token_req.call_args.args
    assert path == "/PROJ/automationSuites/5/builds/9/results/42?expand=testCases"


# --- associate_automation_results ---

def test_associate_results_body_and_synthesized_payload(project, token_req):
    out = associate_automation_results(5, 9, result_ids=[42, 43], test_case_ids=[143])
    method, path = token_req.call_args.args
    assert (method, path) == ("POST", "/PROJ/automationSuites/5/builds/9/results/associate")
    assert token_req.call_args.kwargs["body"] == {
        "resultIDs": [42, 43], "testCaseIDs": [143], "addToSuite": False
    }
    assert json.loads(out) == {
        "associated": True,
        "automation_suite_id": 5,
        "build_id": 9,
        "result_ids": [42, 43],
        "test_case_ids": [143],
        "add_to_suite": False,
    }


def test_associate_results_add_to_suite_flag(project, token_req):
    associate_automation_results(5, 9, [42], [143], add_to_suite=True)
    assert token_req.call_args.kwargs["body"]["addToSuite"] is True


def test_associate_results_error_returns_string(project, token_req):
    token_req.side_effect = RuntimeError("HTTP 400 Bad Request: bad ids")
    assert associate_automation_results(5, 9, [42], [143]) == "Error: HTTP 400 Bad Request: bad ids"
