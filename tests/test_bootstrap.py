"""Bootstrap / session tools: credentials, project selection, token refresh,
config export, and version reporting.

Tools are resolved through ``tool_fn`` (see _test_helpers) so the suite works
regardless of fastmcp's ``decorator_mode``. These tools are mostly NOT
project-scoped, so they patch ``_non_token_authenticated_request`` directly
rather than using the ``token_req`` fixture.
"""
import json
from unittest.mock import patch

import perforce_alm_mcp as alm
from _test_helpers import tool_fn

set_basic_credentials = tool_fn("set_basic_credentials")
list_projects = tool_fn("list_projects")
select_project = tool_fn("select_project")
get_active_project = tool_fn("get_active_project")
refresh_token = tool_fn("refresh_token")
export_mcp_entry = tool_fn("export_mcp_entry")
get_versions = tool_fn("get_versions")


# --- set_basic_credentials ---

def test_set_basic_credentials_rejects_non_basic_auth():
    alm._config.update(auth_type="apikey")
    out = set_basic_credentials("u", "p")
    assert out.startswith("Error: set_basic_credentials only applies when auth_type is 'basic'")


def test_set_basic_credentials_rejects_blank_username():
    alm._config.update(auth_type="basic")
    assert set_basic_credentials("", "p") == "Error: username is required."


def test_set_basic_credentials_allows_empty_password(non_token_req):
    # An empty password is valid (e.g. an account configured without one).
    alm._config.update(auth_type="basic")
    non_token_req.return_value = {"projects": []}  # validation call succeeds
    out = set_basic_credentials("u", "")
    assert json.loads(out)["status"] == "ok"
    assert alm._config["username"] == "u"
    assert alm._config["password"] == ""


def test_set_basic_credentials_stores_creds_and_clears_token(non_token_req):
    alm._config.update(
        auth_type="basic", access_token="stale", token_expires_on="2099-01-01T00:00:00+00:00"
    )
    non_token_req.return_value = {"projects": []}  # validation call succeeds
    out = set_basic_credentials("alice", "s3cret")

    assert json.loads(out)["status"] == "ok"
    assert alm._config["username"] == "alice"
    assert alm._config["password"] == "s3cret"
    # Cached token must be dropped so the next call re-auths with the new creds.
    assert alm._config["access_token"] == ""
    assert alm._config["token_expires_on"] == ""
    # Credentials are validated against the server before returning ok.
    method, path = non_token_req.call_args.args
    assert (method, path) == ("GET", "/projects")


def test_set_basic_credentials_reports_auth_failure(non_token_req):
    alm._config.update(auth_type="basic", username="bob", password="old-pw")
    non_token_req.side_effect = RuntimeError("HTTP 401 Unauthorized: bad creds")
    out = set_basic_credentials("alice", "wrong")
    assert out.startswith("Error: Authentication failed")
    assert "have not been stored" in out
    # Verification failed, so the rejected creds are rolled back — prior
    # credentials (if any) are left untouched rather than overwritten.
    assert alm._config["username"] == "bob"
    assert alm._config["password"] == "old-pw"


def test_set_basic_credentials_distinguishes_unreachable_server(non_token_req):
    alm._config.update(auth_type="basic", username="bob", password="old-pw")
    non_token_req.side_effect = RuntimeError("Connection refused")
    out = set_basic_credentials("alice", "s3cret")
    assert out.startswith("Error: Credentials could not be verified")
    assert "have not been stored" in out
    assert "Connection refused" in out
    # Unreachable server doesn't invalidate prior creds — nothing is rolled
    # forward, nothing is lost.
    assert alm._config["username"] == "bob"
    assert alm._config["password"] == "old-pw"


# --- get_versions ---

def test_get_versions_wraps_mcp_and_api_versions(non_token_req):
    non_token_req.return_value = {"restApiVersion": "2026.1"}
    out = get_versions()

    data = json.loads(out)
    assert data["mcp_server_version"] == alm._MCP_VERSION
    assert data["rest_api_version"] == {"restApiVersion": "2026.1"}
    non_token_req.assert_called_once_with("GET", "/versions")


def test_get_versions_returns_error_string_on_failure(non_token_req):
    non_token_req.side_effect = RuntimeError("boom")
    assert get_versions() == "Error: boom"


# --- list_projects ---

def test_list_projects_passes_response_through(non_token_req):
    payload = {"projects": [{"id": 1, "name": "P"}], "projectsLoading": 0}
    non_token_req.return_value = payload
    out = list_projects()
    assert json.loads(out) == payload
    non_token_req.assert_called_once_with("GET", "/projects")


def test_list_projects_adds_note_when_projects_still_loading(non_token_req):
    non_token_req.return_value = {"projects": [{"id": 1, "name": "P"}], "projectsLoading": 2}
    data = json.loads(list_projects())
    assert "2 project(s) still loading" in data["projects_loading_note"]


def test_list_projects_returns_error_string_on_failure(non_token_req):
    non_token_req.side_effect = RuntimeError("HTTP 401 Unauthorized: nope")
    assert list_projects() == "Error: HTTP 401 Unauthorized: nope"


# --- select_project ---

def test_select_project_activates_valid_id_and_clears_token_on_switch(non_token_req):
    alm._current_project = "OLD"
    alm._config.update(access_token="tok", token_expires_on="2099-01-01T00:00:00+00:00")
    non_token_req.return_value = {"projects": [{"id": 5, "name": "Widgets"}]}
    out = select_project("5")

    assert json.loads(out) == {"active_project": "5", "project_name": "Widgets"}
    assert alm._current_project == "5"
    # Switching to a different project discards the old project-scoped token.
    assert alm._config["access_token"] == ""
    assert alm._config["token_expires_on"] == ""


def test_select_project_rejects_unknown_id(non_token_req):
    non_token_req.return_value = {"projects": [{"id": 5, "name": "Widgets"}]}
    out = select_project("99")
    assert out.startswith("Error: project '99' not found")
    assert "5 (Widgets)" in out          # available projects listed in the message
    assert alm._current_project != "99"


def test_select_project_unknown_id_hints_when_projects_loading(non_token_req):
    non_token_req.return_value = {
        "projects": [{"id": 5, "name": "Widgets"}],
        "projectsLoading": 4,
    }
    out = select_project("99")
    assert out.startswith("Error: project '99' not found")
    assert "4 project(s) still loading" in out
    assert "try again in a few minutes" in out.lower()


def test_select_project_reports_fetch_failure(non_token_req):
    non_token_req.side_effect = RuntimeError("HTTP 401 Unauthorized: x")
    assert select_project("5").startswith("Error: fetching projects failed:")


# --- get_active_project ---

def test_get_active_project_reports_current():
    alm._current_project = "PROJ"
    assert json.loads(get_active_project()) == {"active_project": "PROJ"}


def test_get_active_project_none_when_unset():
    alm._current_project = ""
    assert json.loads(get_active_project()) == {"active_project": None}


# --- refresh_token ---

def test_refresh_token_requires_active_project():
    alm._current_project = ""
    assert refresh_token() == "Error: No active project. Call select_project first."


def test_refresh_token_clears_then_refetches():
    alm._current_project = "PROJ"
    alm._config.update(access_token="old", token_expires_on="old-exp")

    def fake_get_token():
        # _get_token would write a fresh token + expiry after refreshing.
        alm._config["access_token"] = "new"
        alm._config["token_expires_on"] = "2099-01-01T00:00:00+00:00"
        return "new"

    with patch.object(alm, "_get_token", side_effect=fake_get_token):
        out = refresh_token()
    assert json.loads(out)["token_expires_on"] == "2099-01-01T00:00:00+00:00"


def test_refresh_token_returns_error_when_refresh_fails():
    alm._current_project = "PROJ"
    with patch.object(alm, "_get_token", side_effect=RuntimeError("token endpoint down")):
        assert refresh_token() == "Error: token endpoint down"


# --- export_mcp_entry ---

def test_export_mcp_entry_wraps_server_entry():
    alm._config.update(
        url="host", port="8443", auth_type="apikey",
        api_key_id="kid", api_key_secret="ksec", access_token="tok-123",
    )
    raw = export_mcp_entry()
    data = json.loads(raw)
    assert list(data["mcpServers"].keys()) == [alm._SERVER_NAME]
    entry = data["mcpServers"][alm._SERVER_NAME]
    assert entry["command"] == "python"
    # Non-secret config passes through verbatim.
    assert entry["env"][alm._ENV_URL] == "host"

    # The id and secret are redacted to a placeholder — never echoed, not
    # even partially.
    for key in (alm._ENV_KEY_ID, alm._ENV_KEY_SECRET):
        assert entry["env"][key] == f"<{key} is redacted. Ask the user for it, or check the PERFORCE_ALM_* environment variables.>"
    # The bearer token is never persisted or exported at all — it's fetched
    # fresh each session, so it shouldn't appear in the entry's env block.
    for secret in ("kid", "ksec", "tok-123"):
        assert secret not in raw


def test_export_mcp_entry_returns_error_string_on_bad_launch_args(monkeypatch):
    # A misconfigured launch-args override makes _launch_invocation raise; the
    # tool must surface it as an "Error: ..." string, not propagate the exception.
    monkeypatch.setenv(alm._ENV_LAUNCH_ARGS, "not-json")
    out = export_mcp_entry()
    assert out.startswith("Error:")


# --- bootstrap instructions ---

def test_build_instructions_tells_ai_to_ask_for_env_vars():
    text = alm._build_instructions()
    assert "Ask the user for the REST API URL" in text
    assert alm._ENV_URL in text
    assert "restarted" in text
    assert "set_basic_credentials" in text
