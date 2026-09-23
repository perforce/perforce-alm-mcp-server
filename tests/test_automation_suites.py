"""Automation-suite tools: list/get/create/update suite, list/add/remove suite
test cases, run_automation_suite, list_automation_suite_builds.

Highlights: create/update sub-object wrapping (testCasesData) and partial-update
semantics, the synthesized confirmation payloads on the 204/DELETE tools
(update_automation_suite, remove_automation_suite_testcase), the jenkins build-
parameter wrapping in run_automation_suite, and the filter/sort/paging query
builder in list_automation_suite_builds.
"""
import json
from urllib.parse import urlparse, parse_qs

import perforce_alm_mcp as alm
from _test_helpers import tool_fn

list_automation_suites = tool_fn("list_automation_suites")
get_automation_suite = tool_fn("get_automation_suite")
create_automation_suites = tool_fn("create_automation_suites")
update_automation_suite = tool_fn("update_automation_suite")
list_automation_suite_testcases = tool_fn("list_automation_suite_testcases")
add_automation_suite_testcases = tool_fn("add_automation_suite_testcases")
remove_automation_suite_testcase = tool_fn("remove_automation_suite_testcase")
run_automation_suite = tool_fn("run_automation_suite")
list_automation_suite_builds = tool_fn("list_automation_suite_builds")


# --- list_automation_suites ---

def test_list_suites_no_filters(project, token_req):
    token_req.return_value = {"automationSuitesData": []}
    list_automation_suites()
    token_req.assert_called_once_with("GET", "/PROJ/automationSuites")


def test_list_suites_with_filters(project, token_req):
    list_automation_suites(test_case_id=143, automation_script_id="TC-1", expand=["testCases"])
    _, path = token_req.call_args.args
    q = parse_qs(urlparse(path).query)
    assert q["testCaseID"] == ["143"]
    assert q["automationScriptID"] == ["TC-1"]
    assert q["expand"] == ["testCases"]


# --- get_automation_suite ---

def test_get_suite_path(project, token_req):
    token_req.return_value = {"id": 5}
    get_automation_suite(5)
    token_req.assert_called_once_with("GET", "/PROJ/automationSuites/5")


def test_get_suite_with_expand(project, token_req):
    get_automation_suite(5, expand=["testCases"])
    _, path = token_req.call_args.args
    assert path == "/PROJ/automationSuites/5?expand=testCases"


# --- create_automation_suites ---

def test_create_suite_minimal(project, token_req):
    token_req.return_value = {"automationSuitesData": [{"id": 1}]}
    create_automation_suites(name="Nightly")
    method, path = token_req.call_args.args
    suites = token_req.call_args.kwargs["body"]["automationSuitesData"]
    assert (method, path) == ("POST", "/PROJ/automationSuites")
    assert len(suites) == 1
    assert suites[0] == {"name": "Nightly", "active": True}


def test_create_suite_full_wraps_testcases(project, token_req):
    create_automation_suites(
        name="Nightly",
        description="d",
        active=False,
        script_id_prefix="TC",
        owners=[{"username": "smithj"}],
        run_configuration={"type": "jenkins"},
        testcases=[{"id": 143}],
    )
    suite = token_req.call_args.kwargs["body"]["automationSuitesData"][0]
    assert suite["description"] == "d"
    assert suite["active"] is False
    assert suite["scriptIDPrefix"] == "TC"
    assert suite["owners"] == [{"username": "smithj"}]
    assert suite["runConfiguration"] == {"type": "jenkins"}
    assert suite["testCases"] == {"testCasesData": [{"id": 143}]}


# --- update_automation_suite ---

def test_update_suite_partial_sends_only_supplied_fields(project, token_req):
    out = update_automation_suite(5, name="Renamed")
    method, path = token_req.call_args.args
    assert (method, path) == ("PUT", "/PROJ/automationSuites/5")
    assert token_req.call_args.kwargs["body"] == {"name": "Renamed"}
    # Synthesized client-side confirmation (endpoint returns 204).
    assert json.loads(out) == {"updated": True, "automation_suite_id": 5}


def test_update_suite_empty_string_clears_but_none_skips(project, token_req):
    update_automation_suite(5, description="", script_id_prefix=None)
    body = token_req.call_args.kwargs["body"]
    assert body == {"description": ""}   # "" clears; None-valued args are omitted


def test_update_suite_wraps_testcases(project, token_req):
    update_automation_suite(5, testcases=[{"id": 9}])
    assert token_req.call_args.kwargs["body"]["testCases"] == {"testCasesData": [{"id": 9}]}


# --- list/add/remove suite test cases ---

def test_list_suite_testcases_path(project, token_req):
    token_req.return_value = {"testCasesData": []}
    list_automation_suite_testcases(5)
    token_req.assert_called_once_with("GET", "/PROJ/automationSuites/5/testCases")


def test_add_suite_testcases_wraps_testcasesdata(project, token_req):
    add_automation_suite_testcases(5, [{"id": 143, "resultUniqueNames": ["x"]}])
    method, path = token_req.call_args.args
    assert (method, path) == ("POST", "/PROJ/automationSuites/5/testCases")
    assert token_req.call_args.kwargs["body"] == {
        "testCasesData": [{"id": 143, "resultUniqueNames": ["x"]}]
    }


def test_remove_suite_testcase_delete_and_synthesized_payload(project, token_req):
    out = remove_automation_suite_testcase(5, 143)
    method, path = token_req.call_args.args
    assert (method, path) == ("DELETE", "/PROJ/automationSuites/5/testCases/143")
    assert json.loads(out) == {
        "deleted": True, "automation_suite_id": 5, "test_case_id": 143
    }


def test_remove_suite_testcase_error_returns_string(project, token_req):
    token_req.side_effect = RuntimeError("HTTP 404 Not Found: gone")
    assert remove_automation_suite_testcase(5, 143) == "Error: HTTP 404 Not Found: gone"


# --- run_automation_suite ---

def test_run_suite_default_jenkins_with_params(project, token_req):
    token_req.return_value = {"id": 167}
    run_automation_suite(
        5,
        test_run_set={"id": 2},
        build_parameters=[{"name": "BRANCH", "type": "text", "text": "main"}],
    )
    method, path = token_req.call_args.args
    body = token_req.call_args.kwargs["body"]
    assert (method, path) == ("POST", "/PROJ/automationSuites/5/run")
    assert body["type"] == "jenkins"
    assert body["testRunSet"] == {"id": 2}
    assert body["jenkins"] == {"buildParameters": [{"name": "BRANCH", "type": "text", "text": "main"}]}


def test_run_suite_non_jenkins_omits_jenkins_block(project, token_req):
    run_automation_suite(5, build_parameters=[{"name": "X"}], config_type="other")
    body = token_req.call_args.kwargs["body"]
    assert body == {"type": "other"}   # build_parameters ignored for non-jenkins


# --- list_automation_suite_builds: query construction ---

def test_list_builds_defaults_have_no_query(project, token_req):
    token_req.return_value = {"buildsData": []}
    list_automation_suite_builds(5)
    token_req.assert_called_once_with("GET", "/PROJ/automationSuites/5/builds")


def test_list_builds_translates_filter_sort_paging_params(project, token_req):
    list_automation_suite_builds(
        5,
        number="167", number_match_type="exact",
        date_min="2025-01-01T00:00:00Z",
        duration_min=1000, results_passed_max=5,
        status=["finished", "building"],
        test_run_sets=["Regression", 3],
        protected=True,
        sort_by="date", sort_order="desc",
        include_total=True, page=2, per_page=50,
    )
    _, path = token_req.call_args.args
    q = parse_qs(urlparse(path).query)
    assert q["number"] == ["167"]
    assert q["number_match_type"] == ["exact"]
    assert q["date[gte]"] == ["2025-01-01T00:00:00Z"]
    assert q["duration[gte]"] == ["1000"]
    assert q["results_passed[lte]"] == ["5"]
    assert q["status"] == ["finished,building"]          # multi-select comma-joined
    assert q["test_run_sets"] == ["Regression,3"]        # mixed str/int joined
    assert q["protected"] == ["true"]
    assert q["sort_by"] == ["date"]
    assert q["sort_order"] == ["desc"]
    assert q["include_total"] == ["true"]
    assert q["page"] == ["2"]
    assert q["per_page"] == ["50"]
