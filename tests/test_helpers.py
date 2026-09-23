"""Pure helper unit tests — the request-construction and telemetry-classification
helpers that the tools rely on but that aren't fully reachable through a single
tool call. (Path/search-body building is also exercised end-to-end via the tool
tests; here we cover the branches a tool can't easily reach: unsupported auth,
filterID, project-segment encoding, and the error classifiers.)
"""
import base64
import types
import urllib.parse

import pytest

import perforce_alm_mcp as alm


# --- _create_authorization_header ---

def test_auth_header_basic_is_base64():
    alm._config.update(auth_type="basic", username="alice", password="s3cret")
    expected = "Basic " + base64.b64encode(b"alice:s3cret").decode()
    assert alm._create_authorization_header() == expected


def test_auth_header_apikey_is_raw_not_base64():
    alm._config.update(auth_type="apikey", api_key_id="kid", api_key_secret="ksec")
    assert alm._create_authorization_header() == "ApiKey kid:ksec"


def test_auth_header_unsupported_auth_raises():
    alm._config.update(auth_type="")
    with pytest.raises(ValueError, match="auth_type"):
        alm._create_authorization_header()


# --- _create_base_restapi_url ---

def test_base_url_adds_https_when_scheme_missing():
    alm._config.update(url="host.example.com", port="8443")
    assert (
        alm._create_base_restapi_url()
        == "https://host.example.com:8443/helix-alm/api/v0"
    )


def test_base_url_preserves_existing_scheme():
    alm._config.update(url="http://host.example.com", port="80")
    assert (
        alm._create_base_restapi_url()
        == "http://host.example.com:80/helix-alm/api/v0"
    )


# --- _project_path ---

def test_project_path_default_suffix():
    alm._current_project = "PROJ"
    assert alm._project_path() == "/PROJ"


def test_project_path_percent_encodes_project_segment():
    alm._current_project = "My Project/v2"   # space and slash must be encoded
    assert alm._project_path("/requirements/3") == "/My%20Project%2Fv2/requirements/3"


# --- _valid_projects ---

def test_valid_projects_drops_entries_without_id():
    data = {"projects": [{"id": 7, "name": "A"}, {"name": "no id"}]}
    assert alm._valid_projects(data) == [{"id": 7, "name": "A"}]


# --- _build_search_body (filterID + defaults; quoting is covered via the tools) ---

def test_build_search_body_defaults_are_empty():
    assert alm._build_search_body() == {}


def test_build_search_body_includes_filter_id():
    body = alm._build_search_body(filter_id="My Filter")
    assert body == {"filterID": "My Filter"}


# --- _launch_invocation ---

def test_launch_invocation_defaults_to_python_and_script(monkeypatch):
    monkeypatch.delenv(alm._ENV_LAUNCH_COMMAND, raising=False)
    monkeypatch.delenv(alm._ENV_LAUNCH_ARGS, raising=False)
    assert alm._launch_invocation() == ("python", [alm._SERVER_SCRIPT])


def test_launch_invocation_honors_overrides(monkeypatch):
    monkeypatch.setenv(alm._ENV_LAUNCH_COMMAND, "docker")
    monkeypatch.setenv(
        alm._ENV_LAUNCH_ARGS, '["run", "-i", "--rm", "perforce-alm-mcp"]'
    )
    assert alm._launch_invocation() == (
        "docker",
        ["run", "-i", "--rm", "perforce-alm-mcp"],
    )


@pytest.mark.parametrize("bad_args", ["not-json", '{"k": "v"}', '"a string"', "42"])
def test_launch_invocation_bad_args_raises(monkeypatch, bad_args):
    # An explicitly-set but malformed/non-array LAUNCH_ARGS is a misconfiguration:
    # raise rather than silently falling back to the (wrong) default script path.
    monkeypatch.setenv(alm._ENV_LAUNCH_COMMAND, "docker")
    monkeypatch.setenv(alm._ENV_LAUNCH_ARGS, bad_args)
    with pytest.raises(ValueError):
        alm._launch_invocation()


# --- _build_server_entry ---

def test_server_entry_reflects_launch_overrides(monkeypatch):
    monkeypatch.setenv(alm._ENV_LAUNCH_COMMAND, "docker")
    monkeypatch.setenv(alm._ENV_LAUNCH_ARGS, '["run", "-i", "--rm", "img"]')
    entry = alm._build_server_entry()
    assert entry["command"] == "docker"
    assert entry["args"] == ["run", "-i", "--rm", "img"]
    # Launch-override vars are host-launch hints, never runtime config.
    assert alm._ENV_LAUNCH_COMMAND not in entry["env"]
    assert alm._ENV_LAUNCH_ARGS not in entry["env"]


def test_server_entry_omits_basic_creds_and_bearer_token():
    alm._config.update(
        auth_type="basic",
        username="basicuser",
        password="basicpass",
        access_token="tok-123",
        token_expires_on="2099-01-01T00:00:00+00:00",
    )
    entry = alm._build_server_entry()
    env = entry["env"]

    # Basic creds must never reach the persisted env block.
    assert alm._ENV_USERNAME not in env
    assert alm._ENV_PASSWORD not in env
    assert "basicuser" not in env.values()
    assert "basicpass" not in env.values()

    # Nor must the bearer token — it's fetched fresh each session, never persisted.
    assert "tok-123" not in env.values()


# --- _upload_attachment_request (multipart/form-data construction) ---

def test_upload_attachment_request_builds_multipart(tmp_path, monkeypatch):
    alm._config.update(url="host.example.com", port="8443", ssl_verify=True)
    monkeypatch.setenv("PERFORCE_ALM_MCP_UPLOAD_DIR", str(tmp_path))
    f = tmp_path / "spec.pdf"
    f.write_bytes(b"PDFDATA\x00\x01")

    captured = {}
    def fake_execute(req, timeout):
        captured["req"] = req
        return {"attachmentsData": [{"id": 9}]}
    monkeypatch.setattr(alm, "_get_token", lambda: "tok")
    monkeypatch.setattr(alm, "_execute_request", fake_execute)

    out = alm._upload_attachment_request("/PROJ/requirements/7/attachments", str(f))
    assert out == {"attachmentsData": [{"id": 9}]}

    req = captured["req"]
    assert req.method == "POST"
    assert req.full_url.endswith("/PROJ/requirements/7/attachments")
    assert req.get_header("Authorization") == "Bearer tok"
    ctype = req.get_header("Content-type")
    assert ctype.startswith("multipart/form-data; boundary=----perforce-alm-mcp-")

    body = req.data
    assert b'name="fileUpload"; filename="spec.pdf"' in body
    assert b"PDFDATA\x00\x01" in body            # raw file bytes are embedded verbatim
    boundary = ctype.split("boundary=")[1]
    assert body.startswith(f"--{boundary}\r\n".encode())
    assert body.endswith(f"\r\n--{boundary}--\r\n".encode())


def test_upload_attachment_request_escapes_special_chars_in_filename(tmp_path, monkeypatch):
    """A literal '"' or '\\' in the filename must be escaped per the
    Content-Disposition quoted-string rules, or it terminates the quoted
    filename early and produces a malformed multipart request. Windows/NTFS
    forbids '"' in filenames, but Linux and macOS (both real deployment
    targets — see the Docker image) do not, so this is faked via basename()
    rather than relying on the host filesystem to allow creating such a file.
    """
    alm._config.update(url="host.example.com", port="8443", ssl_verify=True)
    monkeypatch.setenv("PERFORCE_ALM_MCP_UPLOAD_DIR", str(tmp_path))
    f = tmp_path / "spec.pdf"
    f.write_bytes(b"DATA")

    captured = {}
    def fake_execute(req, timeout):
        captured["req"] = req
        return {}
    monkeypatch.setattr(alm, "_get_token", lambda: "tok")
    monkeypatch.setattr(alm, "_execute_request", fake_execute)
    monkeypatch.setattr(alm.os.path, "basename", lambda p: 'my "special"\\file.pdf')

    alm._upload_attachment_request("/PROJ/requirements/7/attachments", str(f))

    body = captured["req"].data
    assert b'filename="my \\"special\\"\\\\file.pdf"' in body


def test_upload_attachment_request_uses_rfc2231_for_non_ascii_filename(tmp_path, monkeypatch):
    """A non-ASCII filename can't go in a plain quoted-string per RFC 7578 ->
    RFC 2183 (raw UTF-8 bytes are outside the grammar's US-ASCII expectation);
    reproduced live against a real server, which decoded such bytes one at a
    time as Latin-1 and silently stored a corrupted filename. The RFC 2231
    filename*=UTF-8''<percent-encoded> form is used instead for any filename
    that isn't pure ASCII.
    """
    alm._config.update(url="host.example.com", port="8443", ssl_verify=True)
    monkeypatch.setenv("PERFORCE_ALM_MCP_UPLOAD_DIR", str(tmp_path))
    f = tmp_path / "spec.pdf"
    f.write_bytes(b"DATA")

    captured = {}
    def fake_execute(req, timeout):
        captured["req"] = req
        return {}
    monkeypatch.setattr(alm, "_get_token", lambda: "tok")
    monkeypatch.setattr(alm, "_execute_request", fake_execute)
    monkeypatch.setattr(alm.os.path, "basename", lambda p: "测试附件_日本語.txt")

    alm._upload_attachment_request("/PROJ/requirements/7/attachments", str(f))

    body = captured["req"].data
    expected = "filename*=UTF-8''" + urllib.parse.quote("测试附件_日本語.txt", safe="")
    assert expected.encode() in body
    assert b'filename="' not in body


def test_upload_attachment_request_uses_rfc2231_for_crlf_in_filename(tmp_path, monkeypatch):
    """A CR or LF in the filename is ASCII, so it would otherwise take the
    quoted-string branch — but that branch only escapes '\\' and '"', not
    raw line breaks, which would inject a literal \\r\\n into this hand-built
    multipart body (header/multipart injection). Route it through the RFC
    2231 percent-encoded form instead, same as a non-ASCII filename.
    """
    alm._config.update(url="host.example.com", port="8443", ssl_verify=True)
    monkeypatch.setenv("PERFORCE_ALM_MCP_UPLOAD_DIR", str(tmp_path))
    f = tmp_path / "spec.pdf"
    f.write_bytes(b"DATA")

    captured = {}
    def fake_execute(req, timeout):
        captured["req"] = req
        return {}
    monkeypatch.setattr(alm, "_get_token", lambda: "tok")
    monkeypatch.setattr(alm, "_execute_request", fake_execute)
    evil_filename = 'evil.pdf"\r\nContent-Disposition: form-data; name="x'
    monkeypatch.setattr(alm.os.path, "basename", lambda p: evil_filename)

    alm._upload_attachment_request("/PROJ/requirements/7/attachments", str(f))

    body = captured["req"].data
    expected = "filename*=UTF-8''" + urllib.parse.quote(evil_filename, safe="")
    assert expected.encode() in body
    assert b'filename="' not in body
    assert b'Content-Disposition: form-data; name="x' not in body


def test_upload_attachment_request_missing_file_raises(monkeypatch):
    # open() fails before any token/network work.
    monkeypatch.setattr(alm, "_get_token", lambda: "tok")
    with pytest.raises(FileNotFoundError):
        alm._upload_attachment_request("/PROJ/requirements/7/attachments", "no_such_file_xyz.bin")


# --- _token_authenticated_request_with_status (used only by the four update_* tools) ---

def test_token_authenticated_request_with_status_builds_request_and_returns_status(monkeypatch):
    alm._config.update(url="host.example.com", port="8443", ssl_verify=True)
    monkeypatch.setattr(alm, "_get_token", lambda: "tok")
    captured = {}

    def fake_execute_raw(req, timeout, *, with_headers=False, with_status=False):
        captured["req"] = req
        assert with_status is True
        return b'{"issues": [{"id": 1}]}', 206

    monkeypatch.setattr(alm, "_execute_raw_request", fake_execute_raw)

    data, status = alm._token_authenticated_request_with_status("PUT", "/PROJ/issues", {"issues": []})

    assert status == 206
    assert data == {"issues": [{"id": 1}]}
    req = captured["req"]
    assert req.method == "PUT"
    assert req.full_url == "https://host.example.com:8443/helix-alm/api/v0/PROJ/issues"
    assert req.get_header("Authorization") == "Bearer tok"
    assert req.get_header("Accept") == "application/json"
    assert req.get_header("Content-type") == "application/json"


def test_token_authenticated_request_with_status_empty_body_returns_empty_dict(monkeypatch):
    monkeypatch.setattr(alm, "_get_token", lambda: "tok")
    monkeypatch.setattr(alm, "_execute_raw_request", lambda req, timeout, **kw: (b"", 200))
    data, status = alm._token_authenticated_request_with_status("PUT", "/PROJ/issues", {"issues": []})
    assert (data, status) == ({}, 200)


# --- _folders_partial_success_warning (the four update_* tools' 206 signal) ---

def test_folders_warning_none_on_plain_success():
    assert alm._folders_partial_success_warning(200, {"issues": [{"id": 1}]}, had_folders=True) is None


def test_folders_warning_none_without_folders_even_on_206():
    """The warning is scoped to the folders footgun specifically — a 206
    unrelated to `folders` shouldn't claim a folders risk that isn't there."""
    data = {"issues": [], "errors": [{"code": "E", "message": "bad field"}]}
    assert alm._folders_partial_success_warning(206, data, had_folders=False) is None


def test_folders_warning_fires_on_206_with_folders():
    data = {"issues": [], "errors": [{"code": "E", "message": "bad folder"}]}
    warning = alm._folders_partial_success_warning(206, data, had_folders=True)
    assert warning is not None
    assert "WARNING" in warning
    assert "folders" in warning
    assert "206" in warning


def test_folders_warning_fires_on_errors_array_without_206_status():
    """Belt-and-suspenders: a 200 that still carries a non-empty `errors`
    array triggers the same warning as an explicit 206 — the tool shouldn't
    rely solely on the status code matching the errors array."""
    data = {"issues": [], "errors": [{"code": "E", "message": "bad folder"}]}
    assert alm._folders_partial_success_warning(200, data, had_folders=True) is not None


def test_folders_warning_none_on_empty_errors_array():
    data = {"issues": [], "errors": []}
    assert alm._folders_partial_success_warning(200, data, had_folders=True) is None


# --- telemetry classifiers (no args/results/messages recorded; only category) ---

@pytest.mark.parametrize("text, expected", [
    ("Error: HTTP 401 Unauthorized: x", "auth_failed"),
    ("Error: forbidden", "auth_failed"),
    ("Error: HTTP 404 Not Found: x", "not_found"),
    ("Error: HTTP 429 rate limit exceeded", "rate_limited"),
    ("Error: request timed out", "timeout"),
    ("Error: something else entirely", "tool_error"),
])
def test_classify_error_categories(text, expected):
    assert alm._classify_error(text) == expected


def test_result_error_category_reads_leading_error_text():
    block = types.SimpleNamespace(text="Error: HTTP 404 Not Found: gone")
    result = types.SimpleNamespace(content=[block])
    assert alm._result_error_category(result) == "not_found"


def test_result_error_category_none_on_success():
    block = types.SimpleNamespace(text='{"id": 1}')
    result = types.SimpleNamespace(content=[block])
    assert alm._result_error_category(result) is None


def test_result_error_category_none_when_no_content():
    assert alm._result_error_category(types.SimpleNamespace(content=None)) is None


# --- _resolve_otel_endpoint (env-var precedence; falls back to compiled default) ---

def test_resolve_otel_endpoint_falls_back_to_compiled_default(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    # Assert against the constant, not "", so this survives the default being
    # uncommented when telemetry ships on.
    assert alm._resolve_otel_endpoint() == alm._OTEL_ENDPOINT


def test_resolve_otel_endpoint_uses_general_env_over_default(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://gw.example:4317")
    assert alm._resolve_otel_endpoint() == "https://gw.example:4317"


def test_resolve_otel_endpoint_traces_var_wins(monkeypatch):
    # The traces-specific var takes precedence over the general one (OTel precedence).
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://traces.example:4317")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://general.example:4317")
    assert alm._resolve_otel_endpoint() == "https://traces.example:4317"
