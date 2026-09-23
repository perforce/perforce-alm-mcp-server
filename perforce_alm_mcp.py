"""
Perforce ALM MCP Server

An MCP server that wraps the Perforce ALM REST API,
exposing requirements, documents, document trees and snapshots, test cases,
automation suites and build results, and menu configs as MCP tools. Speaks
MCP over stdio via FastMCP; designed to be spawned by an MCP client
rather than run interactively.
"""

import json
import ssl
import base64
import os
import ntpath
import pathlib
import tempfile
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
from typing import Annotated
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware
from mcp.types import ToolAnnotations
from pydantic import BeforeValidator


def _coerce_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


# Some MCP clients (Claude Code among them) flatten a structured argument into a
# JSON string when the parameter's schema has no top-level "type" key — which
# happens whenever an optional object/array param is wrapped in anyOf by `| None`.
# These aliases recover transparently via a BeforeValidator; the advertised
# schema is unchanged, so well-behaved clients are unaffected.
JsonObj = Annotated[dict, BeforeValidator(_coerce_json)]
JsonObjList = Annotated[list[dict], BeforeValidator(_coerce_json)]
JsonStrMap = Annotated[dict[str, str], BeforeValidator(_coerce_json)]
JsonStrList = Annotated[list[str], BeforeValidator(_coerce_json)]
JsonMixedList = Annotated[list[str | int], BeforeValidator(_coerce_json)]

# MCP tool annotations. A client uses these to decide which tool calls need a
# human approval prompt; without them every tool looks the same regardless of
# whether it's a read or a bulk write, so these must stay in sync with what
# each tool actually does. Session/config tools change no Perforce ALM data,
# only this server's own in-memory session state, but several of them still
# make a real ALM API call to validate what they're setting (openWorldHint),
# not just a local mutation.
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True,
)
# Same as _READ_ONLY, but for the handful of tools that touch no network at
# all - pure computation over this server's own in-memory state
# (export_mcp_entry, get_active_project).
_READ_ONLY_LOCAL = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
)
_SESSION_STATE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True,
)
_WRITE_ADDITIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True,
)
# Same as _WRITE_ADDITIVE, but for tools that mutate a set-membership or
# association rather than minting an independent record with its own ID -
# replaying the same call again leaves the same end state (add_automation_
# suite_testcases, associate_automation_results).
_WRITE_ASSOCIATIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True,
)
# "Destructive" here means Perforce ALM data - wholesale-replaces or deletes
# existing ALM records (update_*, remove_automation_suite_testcase). Does NOT
# cover local-filesystem-only writes; see _LOCAL_FILE_WRITE below.
_WRITE_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True,
)
# Same as _WRITE_DESTRUCTIVE, but for update_issues specifically: its
# foundByRecords mechanism treats any record without an "id" as a new
# addition, so replaying an identical call twice adds a second found-by
# record rather than converging on the same state.
_WRITE_DESTRUCTIVE_UNSAFE_RETRY = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True,
)
# Writes to the local machine running this server, not to Perforce ALM -
# download_attachment fetches file bytes from ALM (read-only there) but can
# overwrite an existing local file, so it isn't read-only, yet ALM data is
# never destroyed, so destructiveHint stays False.
_LOCAL_FILE_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True,
)
_TRIGGER = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True,
)

# Global configuration variables
_ENV_URL                = "PERFORCE_ALM_URL"
_ENV_PORT               = "PERFORCE_ALM_PORT"
_ENV_AUTH_TYPE          = "PERFORCE_ALM_AUTH_TYPE"    # "apikey" or "basic"
_ENV_KEY_ID             = "PERFORCE_ALM_API_KEY_ID"
_ENV_KEY_SECRET         = "PERFORCE_ALM_API_KEY_SECRET"
_ENV_USERNAME           = "PERFORCE_ALM_USERNAME"
_ENV_PASSWORD           = "PERFORCE_ALM_PASSWORD"
_ENV_SSL_VERIFY         = "PERFORCE_ALM_SSL_VERIFY"
_ENV_DEFAULT_PROJECT    = "PERFORCE_ALM_DEFAULT_PROJECT"
_ENV_SERVER_NAME        = "PERFORCE_ALM_MCP_SERVER_NAME"
_ENV_LAUNCH_COMMAND     = "PERFORCE_ALM_MCP_LAUNCH_COMMAND"
_ENV_LAUNCH_ARGS        = "PERFORCE_ALM_MCP_LAUNCH_ARGS"
_ENV_CONFIG_FILE        = "PERFORCE_ALM_MCP_CONFIG_FILE"
_ENV_DOWNLOAD_DIR       = "PERFORCE_ALM_MCP_DOWNLOAD_DIR"  # confines download_attachment writes
_ENV_UPLOAD_DIR         = "PERFORCE_ALM_MCP_UPLOAD_DIR"    # confines upload_*_attachment reads
_MAX_LEAF_NAME          = 200  # cap on a sanitized download filename, well under MAX_PATH
_OTEL_SERVICE_NAME = "perforce-alm-mcp"
# Telemetry is Perforce-controlled. Disabled by default while the server is
# still under development — flip _TELEMETRY_ENABLED to True to ship it on. The
# Perforce Agentic Gateway (or any operator) can redirect the export destination
# at runtime via the standard OTel env vars — see _resolve_otel_endpoint();
# absent those, the compiled default below is used.
_TELEMETRY_ENABLED = True
# Compiled default OTLP/gRPC traces endpoint. Telemetry stays off regardless
# (gated by _TELEMETRY_ENABLED above) until that flag flips to True.
_OTEL_ENDPOINT = "https://grpc.public.prd.shared.perforce.com"

_SERVER_SCRIPT = str(pathlib.Path(__file__).resolve())
_MCP_VERSION   = "1.0.0"
_SERVER_NAME   = os.environ.get(_ENV_SERVER_NAME, "Perforce ALM")  # mcpServers key in a caller-saved config file
_HTTP_TIMEOUT  = 30   # default timeout for HTTP calls


def _load_config_file_env() -> dict:
    """Read PERFORCE_ALM_MCP_CONFIG_FILE, if set, and return the saved `env`
    block for this server's entry.

    This server never writes such a file itself — export_mcp_entry returns
    the resolved entry for the caller to persist wherever fits its own
    environment (its own MCP client config, typically). If the caller
    points back at that saved file via this one env var, every individual
    setting below falls back to it when not already present directly in the
    process environment. A missing/unreadable/malformed file is treated the
    same as an absent one — this is a best-effort fallback, not a required
    source of truth.
    """
    path = os.environ.get(_ENV_CONFIG_FILE, "")
    if not path:
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("mcpServers", {}).get(_SERVER_NAME, {}).get("env", {})
    except Exception:
        return {}


_file_env: dict = _load_config_file_env()


def _env_setting(key: str, default: str = "") -> str:
    """Resolve one setting: a direct env var wins, else the loaded config
    file's value for that same key, else `default`."""
    return os.environ.get(key) or _file_env.get(key, default)


# Initialized from the environment (directly, or via PERFORCE_ALM_MCP_CONFIG_FILE)
# on startup. Not reconfigurable at runtime, except username/password via
# set_basic_credentials.
_config: dict = {
    "url":              _env_setting(_ENV_URL),
    "port":             _env_setting(_ENV_PORT),
    "auth_type":        _env_setting(_ENV_AUTH_TYPE),
    "api_key_id":       _env_setting(_ENV_KEY_ID),
    "api_key_secret":   _env_setting(_ENV_KEY_SECRET),
    "username":         _env_setting(_ENV_USERNAME),
    "password":         _env_setting(_ENV_PASSWORD),
    "ssl_verify":       _env_setting(_ENV_SSL_VERIFY, "true").lower() != "false",
    "default_project":  _env_setting(_ENV_DEFAULT_PROJECT),
    # In-memory only for the life of this process — never seeded from an env
    # var or config file, and never written anywhere; a fresh token is
    # fetched each session.
    "access_token":     "",
    "token_expires_on": "",
}

# Active project for the current session
_current_project: str = _env_setting(_ENV_DEFAULT_PROJECT)


# ---------------------------------------------------------------------------
# OpenTelemetry (optional; OFF by default)
#
# Instrumentation always runs, but is a no-op until _init_telemetry() installs
# a TracerProvider — which only happens when telemetry is enabled and an OTLP
# endpoint is resolvable (compiled default or OTEL_EXPORTER_OTLP_* env var).
# One SERVER span per tool call, following the MCP OTel semantic conventions.
# NEVER records tool arguments, results, or error messages — only call shape.
# ---------------------------------------------------------------------------

def _resolve_otel_endpoint() -> str:
    """Effective OTLP endpoint. The Perforce Agentic Gateway (or any operator)
    can redirect telemetry via the standard OTel env vars; absent those, fall
    back to the compiled default. The traces-specific var wins over the general
    one, matching OTel precedence."""
    return (os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
            or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
            or _OTEL_ENDPOINT)


def _init_telemetry() -> None:
    """Configure the OTel SDK + OTLP/gRPC exporter when telemetry is enabled and
    an endpoint is resolvable. No-op (OTel SDK never imported) otherwise. Never
    raises."""
    endpoint = _resolve_otel_endpoint()
    if not (_TELEMETRY_ENABLED and endpoint):
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        resource = Resource.create({
            "service.name":    _OTEL_SERVICE_NAME,
            "service.version": _MCP_VERSION,
        })
        provider = TracerProvider(resource=resource)
        # gRPC only. We resolve/pass the endpoint explicitly; the exporter still
        # reads the other OTEL_EXPORTER_OTLP_* env vars (headers, timeout, TLS).
        # A non-gRPC OTEL_EXPORTER_OTLP_PROTOCOL is intentionally NOT honored.
        exporter = OTLPSpanExporter(endpoint=endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    except Exception:
        # Telemetry must never break the server (missing SDK, bad endpoint, etc.).
        pass


def _classify_error(text: str) -> str:
    """Map an error to a coarse, low-cardinality error.type category. Only the
    category leaves the machine — never the underlying message."""
    low = text.lower()
    if "401" in text or "unauthorized" in low: return "auth_failed"
    if "403" in text or "forbidden" in low:    return "auth_failed"
    if "404" in text or "not found" in low:    return "not_found"
    if "429" in text or "rate limit" in low:   return "rate_limited"
    if "timed out" in low or "timeout" in low: return "timeout"
    return "tool_error"


def _result_error_category(result):
    """Coarse error.type if a tool result signals failure (this server's tools
    return a leading 'Error:' string), else None. Reads only the result's own
    text to classify; records none of it."""
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text.startswith("Error:"):
            return _classify_error(text)
    return None


def _set_client_attributes(span, context) -> None:
    """Best-effort client name/version from the initialize handshake. Never raises."""
    try:
        ctx = getattr(context, "fastmcp_context", None)
        session = ctx.request_context.session if ctx else None
        info = getattr(getattr(session, "client_params", None), "clientInfo", None)
        if info is not None:
            if getattr(info, "name", None):
                span.set_attribute("mcp.client.name", info.name)
            if getattr(info, "version", None):
                span.set_attribute("mcp.client.version", info.version)
    except Exception:
        pass


class _TelemetryMiddleware(Middleware):
    """Wrap every tool call in a SERVER span using MCP OTel semantic conventions.
    No-op overhead when no TracerProvider is configured (telemetry disabled)."""

    async def on_call_tool(self, context, call_next):
        from opentelemetry import trace, propagate
        from opentelemetry.trace import SpanKind, Status, StatusCode

        tool_name = getattr(context.message, "name", "") or ""

        # Adopt any inbound W3C trace context (stdio callers inject it into _meta).
        parent = None
        try:
            meta = getattr(context.message, "meta", None)
            if meta is not None:
                carrier = meta.model_dump() if hasattr(meta, "model_dump") else dict(meta)
                carrier = {k: carrier[k] for k in ("traceparent", "tracestate") if carrier.get(k)}
                if carrier:
                    parent = propagate.extract(carrier)
        except Exception:
            parent = None

        tracer = trace.get_tracer(_OTEL_SERVICE_NAME, _MCP_VERSION)
        with tracer.start_as_current_span(
            f"tools/call {tool_name}", context=parent, kind=SpanKind.SERVER
        ) as span:
            span.set_attribute("mcp.method.name", "tools/call")
            span.set_attribute("gen_ai.operation.name", "execute_tool")
            span.set_attribute("gen_ai.tool.name", tool_name)
            span.set_attribute("network.transport", "pipe")
            _set_client_attributes(span, context)
            try:
                result = await call_next(context)
            except Exception as e:
                span.set_attribute("error.type", _classify_error(str(e)))
                span.set_status(Status(StatusCode.ERROR))
                raise
            category = _result_error_category(result)
            if category:
                span.set_attribute("error.type", category)
                span.set_status(Status(StatusCode.ERROR))
            return result


def _build_instructions() -> str:
    """Build the FastMCP `instructions` string."""
    return (
        "This server wraps the Perforce ALM REST API (requirements, "
        "documents, test cases, automation suites and build results, "
        "menu configs). Most tools are project-scoped and gated behind "
        "an active project.\n"
        "\n"
        "Bootstrap (in order, before any other tool):\n"
        "1. Connection settings are managed via environment variables. Ask the "
        "user for the REST API URL, port, auth type ('apikey' or 'basic'), and "
        "credentials (API key id + secret, or username + password), then set "
        "them as environment variables in the MCP client's configuration for "
        "this server: PERFORCE_ALM_URL, PERFORCE_ALM_PORT, PERFORCE_ALM_AUTH_TYPE, "
        "and either PERFORCE_ALM_API_KEY_ID + PERFORCE_ALM_API_KEY_SECRET or "
        "PERFORCE_ALM_USERNAME + PERFORCE_ALM_PASSWORD. PERFORCE_ALM_SSL_VERIFY "
        "(defaults true) and PERFORCE_ALM_DEFAULT_PROJECT are optional. "
        "Alternatively, if the user has a previously saved config file, they "
        "can set PERFORCE_ALM_MCP_CONFIG_FILE to its path instead of setting "
        "each variable individually. The server must be restarted after these "
        "are set for them to take effect. If auth_type is 'basic' and no "
        "username/password are loaded (i.e. not in PERFORCE_ALM_USERNAME / "
        "PERFORCE_ALM_PASSWORD env vars and not yet supplied this session), "
        "prompt the user for them and call set_basic_credentials instead of "
        "restarting the server — basic-auth creds are always session-only.\n"
        "2. Call get_active_project. If it returns no active project, "
        "call list_projects, present the names to the user, and call "
        "select_project with their choice.\n"
        "\n"
        "Other rules:\n"
        "- On 401 / authentication errors mid-session, call refresh_token "
        "once and retry — the cached bearer token can be dead server-side "
        "(e.g. after an ALM Server restart) even when its local expiry has "
        "not lapsed.\n"
        "- If the user asks to re-initialize or reconfigure, ask them for the "
        "new values and update the same environment variables (or the saved "
        "config file), then restart the server.\n"
        "- The version and export_mcp_entry tools are exempt from the "
        "project-active gate and can be called any time.\n"
        "- Always refer to this product as 'Perforce ALM'\n"
    )


# Server object with instructions for AI
mcp = FastMCP(
    "Perforce ALM",
    middleware=[_TelemetryMiddleware()],
    instructions=_build_instructions(),
)


@mcp.tool(annotations=_SESSION_STATE)
def set_basic_credentials(username: str, password: str) -> str:
    """
    Provide username and password for basic authentication, for the current
    session only. Use this at session start (after the configured auth_type
    is "basic") to supply credentials that were NOT loaded from env vars or
    persisted from a prior session.

    Stored in memory only — never written to disk anywhere. Re-prompt the
    user and call this again on each new session if basic auth is in use.

    The credentials are verified against the server (GET /projects) before this
    returns and before they are stored: if verification fails for any reason,
    nothing is written and whatever credentials (if any) were previously in
    effect are left untouched. Because of that check this makes one network
    round-trip — it is not a purely local call and will block briefly (or
    longer, if the server is slow to respond).

    Errors if the current auth_type is not "basic" — auth_type is set via the
    PERFORCE_ALM_AUTH_TYPE environment variable; change it and restart the
    server to switch authentication methods.

    Args:
        username: Perforce ALM username.
        password: Perforce ALM password. May be empty for an account
                  configured without one.
    """
    if (_config.get("auth_type") or "").lower() != "basic":
        return "Error: set_basic_credentials only applies when auth_type is 'basic'. Set the PERFORCE_ALM_AUTH_TYPE environment variable and restart the server to change authentication methods."
    if not username:
        return "Error: username is required."

    prev_username = _config.get("username", "")
    prev_password = _config.get("password", "")

    # Stage the candidate creds so the verification call below (which builds its
    # Basic auth header from _config) is authenticated with them. Not committed
    # until verification succeeds — rolled back otherwise.
    _config["username"] = username
    _config["password"] = password

    try:
        _non_token_authenticated_request("GET", "/projects")
    except Exception as e:
        _config["username"] = prev_username
        _config["password"] = prev_password
        msg = str(e)
        if "401" in msg or "403" in msg:
            return (
                "Error: Authentication failed. The Perforce ALM server rejected the "
                "username and password. They have not been stored. Re-enter your "
                "credentials with set_basic_credentials."
            )
        return (
            f"Error: Credentials could not be verified against the server: {e}. "
            f"They have not been stored. The server may be unreachable. Retry "
            f"set_basic_credentials once it is available."
        )

    # Verified — clear any cached token so the next call re-authenticates with
    # the new creds instead of an old bearer token issued under different ones.
    _config["access_token"]     = ""
    _config["token_expires_on"] = ""

    return json.dumps(
        {
            "status": "ok",
            "auth_type": "basic",
            "note": "Credentials are verified against the server and stored in memory for this session only.",
        },
        indent=2,
    )


def _launch_invocation() -> tuple[str, list[str]]:
    # The (command, args) the *host* MCP client uses to launch this server.
    # Defaults to running this script directly with `python` — correct for a
    # native pip install / direct checkout. In packaging contexts where the
    # host launch differs from the in-process path — Docker (__file__ is a
    # container-internal /app path) or uvx (a transient venv path) — set
    # PERFORCE_ALM_MCP_LAUNCH_COMMAND and PERFORCE_ALM_MCP_LAUNCH_ARGS (a JSON
    # array) so the persisted/exported entry is launchable on the host.
    command = os.environ.get(_ENV_LAUNCH_COMMAND) or "python"
    args_raw = os.environ.get(_ENV_LAUNCH_ARGS)
    if args_raw is None:
        return command, [_SERVER_SCRIPT]
    # The override was explicitly set, so a malformed value is a misconfiguration:
    # surface it rather than silently emitting the default script path — the very
    # launch line this override exists to replace, and wrong on the host in the
    # Docker/uvx contexts where the var is used.
    try:
        parsed = json.loads(args_raw)
    except json.JSONDecodeError as e:
        raise ValueError(
            f'{_ENV_LAUNCH_ARGS} is not valid JSON. ({e}): expected a JSON array of '
            f'launch arguments such as ["run","-i","--rm","perforce-alm-mcp"].'
        )
    if not isinstance(parsed, list):
        raise ValueError(
            f"{_ENV_LAUNCH_ARGS} must be a JSON array of launch arguments. Received "
            f"{type(parsed).__name__}."
        )
    return command, [str(a) for a in parsed]


def _build_server_entry() -> dict:
    """Builds a standard `{command, args, env}` entry for an MCP client's server
    list, from this process's current in-memory settings.

    This server never writes this entry to disk itself — export_mcp_entry hands
    it back to the caller, which is responsible for persisting it (if the user
    wants that) wherever fits its own MCP client.
    """

    command, args = _launch_invocation()
    return {
        "command": command,
        "args": args,
        "env": {
            _ENV_SERVER_NAME:      _SERVER_NAME,
            _ENV_URL:              _config.get("url", ""),
            _ENV_PORT:             _config.get("port", ""),
            _ENV_AUTH_TYPE:        _config.get("auth_type", ""),
            _ENV_KEY_ID:           _config.get("api_key_id", ""),
            _ENV_KEY_SECRET:       _config.get("api_key_secret", ""),
            _ENV_SSL_VERIFY:       str(_config.get("ssl_verify", True)).lower(),
            _ENV_DEFAULT_PROJECT:  _config.get("default_project", ""),
        },
    }


# Long-lived/live credentials that must never flow into LLM context via
# export_mcp_entry. The API key id/secret are only ever set via environment
# variables, outside any session this tool could be called from, so it never
# has a legitimate reason to echo them back. (Basic-auth creds and the bearer
# token are already excluded from the entry entirely — the token is
# session-only and neither is ever written anywhere.)
_EXPORT_REDACTED_ENV_KEYS = (_ENV_KEY_ID, _ENV_KEY_SECRET)


def _redacted_server_entry() -> dict:
    """_build_server_entry() with live credentials masked, safe to surface to a client/LLM."""
    entry = _build_server_entry()
    for key in _EXPORT_REDACTED_ENV_KEYS:
        if entry["env"].get(key):
            entry["env"][key] = f"<{key} is redacted. Ask the user for it, or check the PERFORCE_ALM_* environment variables.>"
    return entry


def _valid_projects(data: dict) -> list[dict]:
    """Filter a list of Projects to make sure only those with IDs are included"""
    return [p for p in data.get("projects", []) if "id" in p]


def _create_authorization_header() -> str:
    """
    Create the auth header for HTTP requests, depending on whether the auth type
    is 'basic' or 'apikey'. Used in authenticated requests that don't require a bearer token.
    """
    if _config.get("auth_type") == "basic":
        token = base64.b64encode(
            f"{_config['username']}:{_config['password']}".encode()
        ).decode()
        return f"Basic {token}"
    elif _config.get("auth_type") == "apikey":
        return f"ApiKey {_config['api_key_id']}:{_config['api_key_secret']}"
    else:
        raise ValueError(
            f"Unsupported or missing auth_type: {_config.get('auth_type')!r}. "
            "Set the PERFORCE_ALM_AUTH_TYPE environment variable (and related "
            "credentials) and restart the server."
        )


def _create_base_restapi_url() -> str:
    """Build the REST API base URL. Falls back to https:// if the configured URL lacks a scheme"""
    url = _config.get("url", "")
    if not url.lower().startswith(("http://", "https://")):
        url = f"https://{url}"
    # Use the legacy 'helix-alm' path segment, not 'perforce-alm'. ALM 2026.1+
    # rewrites 'helix-alm' -> 'perforce-alm' server-side for backward compat, so
    # 'helix-alm' works on every version while 'perforce-alm' works only on
    # 2026.1+. This may be updated back to 'perforce-alm' in the future (e.g.
    # once pre-2026.1 versions are no longer supported).
    return f"{url}:{_config['port']}/helix-alm/api/v0"


def _project_path(suffix: str = "") -> str:
    """Build a project-scoped REST path, percent-encoding the active project
    segment — the project identifier can contain spaces or other characters
    unsafe in a URL path segment. This is the single place project-scoped paths
    are assembled. `suffix` is the already-safe remainder (literal resource names
    + integer IDs), e.g. "/requirements/42".
    """
    return f"/{urllib.parse.quote(_current_project, safe='')}{suffix}"


def _build_search_body(
    filter_id: str = "",
    search: str = "",
    filters: dict[str, str] | None = None,
    fields: list[str] | None = None,
    page: int = 1,
    per_page: int = 300,
    formatted_text: bool = True,
    expand: list[str] | None = None,
) -> dict:
    """Build the POST body shared by the *_by_query search tools. Combines the
    `filters` shorthand (each pair double-quotes its label and quotes its value,
    choosing single or double quotes so any literal quote in the value is
    preserved) with any raw `search` expression via `and`, then includes only the
    non-default request parameters. Raises ValueError if a filter value contains
    both a single and a double quote (the search syntax cannot express that).
    """
    clauses: list[str] = []
    if filters:
        for label, value in filters.items():
            value = str(value)
            # The server's parser has no escape sequence for a quote inside a
            # string literal — doubling (e.g. '') is NOT supported. The only way
            # to include a literal quote is to wrap the value in the OTHER quote
            # character: a value containing ' must be double-quoted, a value
            # containing " must be single-quoted. A value with both cannot be
            # expressed and is rejected here.
            has_single = "'" in value
            has_double = '"' in value
            if has_single and has_double:
                raise ValueError(
                    f"Cannot search for {label!r}. The value contains both a single "
                    f"quote and a double quote, which the search syntax cannot process. "
                    f"Narrow the search to a substring without one of the quote characters."
                )
            quote = '"' if has_single else "'"
            clauses.append(f'"{label}" = {quote}{value}{quote}')
    if search:
        clauses.append(f"({search})" if clauses else search)
    effective_search = " and ".join(clauses)

    body: dict = {}
    if filter_id:
        body["filterID"] = filter_id
    if effective_search:
        body["search"] = effective_search
    if fields:
        body["fields"] = fields
    if page != 1:
        body["page"] = page
    if per_page != 300:
        body["per_page"] = per_page
    if not formatted_text:
        body["formattedText"] = False
    if expand:
        body["expand"] = expand
    return body


def _send_request(method: str, path: str, authorization: str, body=None, timeout: float = _HTTP_TIMEOUT) -> dict:
    """
    Shared HTTP plumbing for the REST API: build the URL, attach the given
    Authorization header plus JSON Accept/Content-Type, serialize an optional
    JSON body, apply SSL settings, and return the parsed JSON response (or {} on
    an empty body). Normalizes HTTPError into a RuntimeError carrying the
    response body. Callers pass the Authorization header so this stays auth-scheme
    agnostic; this is the single place to add retries/logging later.
    """
    url = f"{_create_base_restapi_url()}{path}"
    data = json.dumps(body).encode() if body is not None else None

    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", authorization)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")

    return _execute_request(req, timeout)


def _execute_raw_request(
    req: urllib.request.Request,
    timeout: float,
    *,
    with_headers: bool = False,
    with_status: bool = False,
):
    """
    Apply SSL settings, send the prepared request, and return the raw response
    bytes, optionally paired with the response headers and/or HTTP status code:
    plain bytes by default; (bytes, response_headers) when with_headers=True;
    (bytes, status) when with_status=True; (bytes, response_headers, status)
    when both are True. headers is an http.client.HTTPMessage, needed by
    _download_file_request to read Content-Disposition. status is the int
    HTTP status code (e.g. 206), needed by
    _token_authenticated_request_with_status since a 2xx status other than
    200/201 can't be told apart from the body alone — note 206 is NOT an
    HTTPError (only >=400 raises), so it flows through the success branch
    below. Normalizes HTTPError into a RuntimeError carrying the response
    body. Shared by _execute_request (JSON) and _download_file_request
    (binary) so SSL handling and error normalization live in one place.
    """
    ctx = ssl.create_default_context()
    if not _config.get("ssl_verify", True):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            body = resp.read()
            result: tuple = (body,)
            if with_headers:
                result += (resp.headers,)
            if with_status:
                result += (resp.status,)
            return result if len(result) > 1 else result[0]
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {body}") from None


def _execute_request(req: urllib.request.Request, timeout: float) -> dict:
    """
    Send the prepared request via _execute_raw_request and parse the response
    body as JSON (or {} on an empty body). Shared by _send_request (JSON) and
    _upload_attachment_request (multipart).
    """
    raw = _execute_raw_request(req, timeout)
    return json.loads(raw.decode()) if raw else {}


def _non_token_authenticated_request(method: str, path: str, timeout: float = _HTTP_TIMEOUT) -> dict:
    """
    HTTP request using API key / basic auth (no bearer token).
    Use for /projects, /versions, and /{projectID}/token (bearer token generation).
    """
    return _send_request(method, path, _create_authorization_header(), timeout=timeout)


def _token_authenticated_request(method: str, path: str, body=None, timeout: float = _HTTP_TIMEOUT) -> dict:
    """HTTP request using Bearer token. Use for all project-scoped endpoints."""
    return _send_request(method, path, f"Bearer {_get_token()}", body=body, timeout=timeout)


def _token_authenticated_request_with_status(
    method: str, path: str, body: dict, timeout: float = _HTTP_TIMEOUT
) -> tuple[dict, int]:
    """Same request shape as _token_authenticated_request, but also returns the
    HTTP status code as (dict, int) so the caller can detect a 206
    partial-success response. Hand-builds the request instead of going
    through _send_request (a few duplicated lines) so that
    _token_authenticated_request's shared, dict-only return contract — relied
    on by every other project-scoped tool and by the `token_req` test fixture
    — stays untouched.
    """
    url = f"{_create_base_restapi_url()}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_get_token()}")
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")
    raw, status = _execute_raw_request(req, timeout, with_status=True)
    return (json.loads(raw.decode()) if raw else {}), status


def _folders_partial_success_warning(
    status: int, data: dict, had_folders: bool, is_create: bool = False
) -> str | None:
    """Build a warning to prepend to an update_*/create_* tool's JSON
    response, or None when no warning is warranted.

    Triggers only when BOTH hold:
      - the request reported a partial success: status == 206, or the
        response body carries a non-empty top-level `errors` array (206 is
        the documented signal, but the errors array is also checked in case
        a response carries errors without matching status)
      - at least one item in the request included a `folders` sub-object

    The REST API's folders handling is NOT atomic, on both PUT (update) and
    POST (create): an invalid folder path/ID doesn't cleanly fail the whole
    request, and the server does not stop processing at the first invalid
    entry — every valid entry in the list is applied regardless of its
    position relative to an invalid one, which is simply skipped and
    reported in `errors` (confirmed empirically: a folders list of
    [valid, invalid, valid] applies both valid entries, not just the one
    before the invalid entry). On PUT, the server has already removed the
    item's existing placements first, so a failed call can leave the item
    filed in some, all, or none of the requested folders, with the original
    placements already gone regardless. On POST there's no prior placement
    to lose, but the same "every valid entry is applied, the item still
    gets created anyway" behavior applies — a create can succeed (id
    assigned) while its requested folders only partially land. Silently
    passing the response through in this case would lose that signal, so
    this is a deliberate, documented break from the project's usual
    verbatim-passthrough convention (see CLAUDE.md's Tool output
    conventions). `is_create` only changes the wording of the message, not
    the trigger logic.
    """
    partial = status == 206 or bool(data.get("errors"))
    if not (partial and had_folders):
        return None
    if is_create:
        return (
            f"WARNING: Partial success (HTTP {status}) for a create request "
            "that included `folders`.\n"
            "The item may have been created even though one or more folder "
            "additions failed (see `errors` below). Folder processing "
            "during creation is not atomic. The item might exist in all, "
            "some, or none of the requested folders. Re-fetch the item "
            "with `expand=[\"folders\"]` to see which folders currently "
            "contain the item. Do not assume the requested folder "
            "additions succeeded."
        )
    return (
        f"WARNING: Partial success (HTTP {status}) for a request that "
        "included `folders`.\n"
        "Folder replacement is not atomic. Existing folder placements are "
        "removed before the requested folders are processed. Invalid "
        "folders are skipped and reported in `errors`, while valid folders "
        "continue to be applied. As a result, an item may end up in all, "
        "some, or none of the requested folders, and its original folder "
        "placements may no longer exist. Re-fetch the affected items with "
        "`expand=[\"folders\"]` to verify their current folder placements. "
        "Do not assume the requested folder changes were applied."
    )


def _confined_root(env_var: str, sandbox_name: str) -> str:
    """Absolute directory that attachment reads/writes are confined to.

    Secure-by-default: overridable via env_var; defaults to a namespaced
    folder under the system temp directory when unset. Read at call time, so
    no new mutable module global."""
    configured = os.environ.get(env_var, "").strip()
    root = configured or os.path.join(tempfile.gettempdir(), sandbox_name)
    return os.path.realpath(root)


def _download_root() -> str:
    return _confined_root(_ENV_DOWNLOAD_DIR, "perforce-alm-mcp-downloads")


def _upload_root() -> str:
    return _confined_root(_ENV_UPLOAD_DIR, "perforce-alm-mcp-uploads")


def _resolve_under_root(root: str, subpath: str) -> str:
    """Resolve `subpath` under `root` and verify the result stays inside `root`
    (realpath also neutralizes symlink escapes). Raises ValueError on escape —
    traversal, an absolute path outside root, or a different Windows drive."""
    resolved = os.path.realpath(os.path.join(root, subpath))
    try:
        inside = os.path.commonpath([root, resolved]) == root
    except ValueError:
        inside = False
    if not inside:
        raise ValueError(f"{subpath!r} resolves outside the permitted directory: {root!r}")
    return resolved


def _safe_leaf_name(name: str | None, fallback: str) -> str:
    """Reduce an untrusted name to a bare leaf filename: no separators, no
    traversal, no NTFS alternate-data-stream suffix, no Windows-reserved
    device name. Falls back to `fallback` when `name` is None, empty,
    whitespace-only, ".", "..", contains a NUL, or is itself reserved
    (e.g. "NUL", "con.txt", "LPT1"). Uses ntpath on every platform (not just
    Windows) so a Docker/Linux deployment and a native Windows install reduce
    a name identically — posixpath.basename would let a `..\\..\\win.ini`
    payload survive intact on Linux, since it doesn't treat backslash as a
    separator.
    """
    leaf = ntpath.basename((name or "").replace("\\", "/")).strip()
    leaf = leaf.split(":", 1)[0].strip()  # drop an NTFS alternate-data-stream suffix
    if len(leaf) > _MAX_LEAF_NAME:
        stem, ext = os.path.splitext(leaf)
        leaf = stem[:_MAX_LEAF_NAME - len(ext)] + ext
    if not leaf or leaf in (".", "..") or "\x00" in leaf or ntpath.isreserved(leaf):
        return fallback
    return leaf


def _upload_attachment_request(path: str, file_path: str, timeout: float = _HTTP_TIMEOUT) -> dict:
    """POST a local file to an /attachments endpoint as multipart/form-data (the
    field name the REST API expects is 'fileUpload'), authenticated with a Bearer
    token. Attachment upload is the only endpoint family that isn't
    application/json, so it can't go through _send_request. Reads the file from
    the machine running this server, confined to the configured upload root
    (PERFORCE_ALM_MCP_UPLOAD_DIR); raises on a path outside that root or a
    missing/unreadable file."""
    resolved_path = _resolve_under_root(_upload_root(), file_path)
    with open(resolved_path, "rb") as fh:
        file_bytes = fh.read()
    filename = os.path.basename(resolved_path) or "attachment"
    # A plain quoted-string filename is US-ASCII only per RFC 7578 -> RFC 2183;
    # raw UTF-8 bytes dropped in there get decoded byte-by-byte as Latin-1 by at
    # least one real server, silently corrupting the stored name. A CR/LF in the
    # filename is also routed away from the quoted-string form, since it isn't
    # escapable there and would otherwise inject a raw line break into this
    # hand-built multipart body. Both cases use the RFC 2231
    # filename*=UTF-8''<percent-encoded> form instead, which percent-encodes
    # every such byte.
    if filename.isascii() and not any(c in filename for c in "\r\n"):
        escaped = filename.replace("\\", "\\\\").replace('"', '\\"')
        disposition = f'filename="{escaped}"'
    else:
        disposition = f"filename*=UTF-8''{urllib.parse.quote(filename, safe='')}"
    boundary = "----perforce-alm-mcp-" + os.urandom(16).hex()
    preamble = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="fileUpload"; {disposition}\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    body = preamble + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    req = urllib.request.Request(f"{_create_base_restapi_url()}{path}", data=body, method="POST")
    req.add_header("Authorization", f"Bearer {_get_token()}")
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    return _execute_request(req, timeout)


def _strip_attachment_content(data: dict) -> dict:
    """Drop the unusable `content` href from each attachmentsData entry: it's a
    REST API URL the caller has no bearer token to fetch. `encodedFileID` is the
    field callers can actually act on, via download_attachment."""
    for attachment in data.get("attachmentsData", []):
        attachment.pop("content", None)
    return data


def _strip_expanded_attachments(item: dict) -> dict:
    """Same fixup as _strip_attachment_content, but for an item's expand-gated
    `attachments` sub-object (present when a get_*/get_*_by_query call passes
    expand=["attachments"]) rather than a list/upload attachment response."""
    attachments = item.get("attachments")
    if attachments:
        _strip_attachment_content(attachments)
    return item


def _strip_issue_expanded_attachments(issue: dict) -> dict:
    """Same fixup as _strip_expanded_attachments, plus the issue-only case:
    each found-by record (present when expand=["foundByRecords"]) carries its
    own nested `attachments` container, separate from the issue-level one."""
    _strip_expanded_attachments(issue)
    found_by_records = issue.get("foundByRecords")
    if found_by_records:
        for record in found_by_records.get("foundByRecordsData", []):
            _strip_expanded_attachments(record)
    return issue


def _download_file_request(path: str, timeout: float = _HTTP_TIMEOUT) -> tuple:
    """GET a binary file from /{projectID}/files/{encodedFileID}, authenticated
    with a Bearer token. Returns (raw_bytes, filename), where filename is
    parsed from the response's Content-Disposition header (None if absent or
    unparseable). The REST spec declares this endpoint's response content type
    as application/json, but the body is actually raw binary, so it can't go
    through _send_request's JSON decode path."""
    req = urllib.request.Request(f"{_create_base_restapi_url()}{path}", method="GET")
    req.add_header("Authorization", f"Bearer {_get_token()}")
    body, headers = _execute_raw_request(req, timeout, with_headers=True)
    return body, headers.get_filename()


def _get_token() -> str:
    """Return a valid Bearer access token for the active project, refreshing if needed.

    Cached in memory only for the life of this process — never written to
    disk — so a new session always fetches a fresh token.
    """
    token      = _config.get("access_token", "")
    expires_on = _config.get("token_expires_on", "")
    if token and expires_on:
        try:
            exp = datetime.fromisoformat(expires_on)
            if exp > datetime.now(timezone.utc):
                return token
        except Exception:
            pass

    if not _current_project:
        raise RuntimeError("No active project. Call select_project before token-scoped requests.")

    data = _non_token_authenticated_request("GET", _project_path("/token"))

    access_token = data.get("accessToken")

    # If for some reason we didn't get a token, raise an error
    if not access_token:
        raise RuntimeError(f"Token endpoint did not return accessToken: {data}")

    _config["access_token"]     = access_token
    _config["token_expires_on"] = data.get("expiresOn", "")

    return _config["access_token"]


@mcp.tool(annotations=_READ_ONLY)
def get_versions() -> str:
    """Return the MCP server version and the Perforce ALM REST API version."""
    try:
        api_data = _non_token_authenticated_request("GET", "/versions")
    except Exception as e:
        return f"Error: {e}"
    return json.dumps({
        "mcp_server_version": _MCP_VERSION,
        "rest_api_version": api_data,
    }, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def list_projects() -> str:
    """
    Return all projects accessible with the current credentials.

    Response: {
      "projects": [{"id": <int>, "name": <str>, "uuid": <str>}, ...],
      "projectsLoading": <int>
    }
    Chain project `id` values into `select_project`.
    """
    try:
        data = _non_token_authenticated_request("GET", "/projects")
    except Exception as e:
        return f"Error: {e}"
    loading = data.get("projectsLoading", 0)
    if loading:
        data["projects_loading_note"] = (
            f"{loading} project(s) still loading on the server. The list may be "
            "incomplete. Try again in a few minutes if an expected project is missing."
        )
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_SESSION_STATE)
def select_project(project_id: str) -> str:
    """
    Set the active project for the current session after validating the ID
    against the list of accessible projects. All subsequent item-level requests
    will use this project ID.

    Args:
        project_id: The project ID to activate (obtain from list_projects).
    """
    global _current_project
    try:
        data = _non_token_authenticated_request("GET", "/projects")
    except Exception as e:
        return f"Error: fetching projects failed: {e}"

    projects = _valid_projects(data)
    valid_ids = {str(p["id"]): p.get("name", "(unnamed)") for p in projects}
    if project_id not in valid_ids:
        names = ", ".join(
            f"{p['id']} ({p.get('name', '(unnamed)')})" for p in projects
        )
        loading = data.get("projectsLoading", 0)
        hint = (
            f" ({loading} project(s) still loading on the server. '{project_id}' may be"
            " valid but not yet available. Try again in a few minutes.)"
            if loading else ""
        )
        return f"Error: project '{project_id}' not found. Available: {names or 'none'}{hint}"

    if project_id != _current_project:
        # Tokens are project-scoped: discard when switching to a different project.
        _config["access_token"]     = ""
        _config["token_expires_on"] = ""
    _current_project = project_id
    return json.dumps({
        "active_project": _current_project,
        "project_name": valid_ids[project_id],
    }, indent=2)


@mcp.tool(annotations=_READ_ONLY_LOCAL)
def get_active_project() -> str:
    """Return the project ID currently active for this session."""
    return json.dumps({"active_project": _current_project or None}, indent=2)


@mcp.tool(annotations=_SESSION_STATE)
def refresh_token() -> str:
    """
    Force a new Bearer access token to be issued for the active project,
    discarding any cached token. Useful when the ALM server has been restarted
    and invalidated the cached token before its local `token_expires_on` has
    lapsed, or when you want to rotate credentials mid-session.
    """
    if not _current_project:
        return "Error: No active project. Call select_project first."
    _config["access_token"]     = ""
    _config["token_expires_on"] = ""
    try:
        _get_token()
    except Exception as e:
        return f"Error: {e}"
    return json.dumps({
        "token_expires_on": _config.get("token_expires_on", ""),
    }, indent=2)


@mcp.tool(annotations=_READ_ONLY_LOCAL)
def export_mcp_entry() -> str:
    """
    Return the current MCP server entry as a JSON snippet ready to paste into
    any MCP client's config file (Claude Desktop, Codex, Cursor, Cline, etc.).

    The entry follows the de-facto-standard `{"command", "args", "env"}` shape
    under the `mcpServers` key, built from this process's current in-memory
    settings. This server never persists configuration to disk itself, so
    this reflects only what's active for the current session.

    The API key id and secret are redacted from its output, since a tool
    response can flow into the AI's own context — ask the user for the real
    value directly if it's needed. The bearer access token is never included
    at all — it's fetched fresh each session. Never echo any portion of a
    redacted credential value (API key id/secret, access token, password)
    back to the user — not a prefix, suffix, or truncated/masked form.
    """
    try:
        return json.dumps({"mcpServers": {_SERVER_NAME: _redacted_server_entry()}}, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(annotations=_READ_ONLY)
def get_requirement(
    item_id: int,
    fields: JsonStrList | None = None,
    formatted_text: bool = True,
    expand: JsonStrList | None = None,
    version: int = 0,
) -> str:
    """
    Get a single requirement by item ID. Wraps GET /{projectID}/requirements/{itemID}.

    Args:
        item_id:        Record ID of the requirement.
        fields:         Field labels to return (case-insensitive, e.g. "Summary",
                        "Product"). Top-level props (id, number, tag) are always
                        returned and don't need listing.
        formatted_text: Return multi-line text as HTML (True) or plain text (False).
        expand:         Sub-objects to expand. Allowed values: attachments,
                        events, documents, versions, links, folders.
        version:        Requirement version (0 = current).

    Response: {
      "id": <int>, "number": <int>, "tag": <str>,
      "self": <str>, "ttstudioURL": <str>, "httpURL": <str>,
      "fields": [{"id": <int>, "label": <str>, "type": <str>, "<type>": <value>}, ...],
      # each field's value sits under a key named by its `type`, one of: string,
      # formattedString, menuItem, menuItemArray, editableVersion, boolean,
      # integer, decimal, date, dateTime, user, userArray
      # (e.g. {"id": 2, "label": "Summary", "type": "string", "string": "Login page"})
      "requirementType": {"id": <int>, "label": <str>},
      # expand-gated, each present only when listed in `expand`:
      "attachments": <obj>, "events": <obj>, "documents": <obj>,
      "versions": <obj>, "links": <obj>, "folders": <obj>
    }
    Chain `id` into update_requirements, or back into get_requirement with a
    different `version` to fetch historical versions.
    """
    params: list[tuple[str, str]] = []
    if fields:
        params.extend(("fields", f) for f in fields)
    if not formatted_text:
        params.append(("formattedText", "false"))
    if expand:
        params.extend(("expand", e) for e in expand)
    if version != 0:
        params.append(("version", str(version)))

    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    path = _project_path(f"/requirements/{item_id}{query}")

    try:
        data = _token_authenticated_request("GET", path)
    except Exception as e:
        return f"Error: {e}"
    _strip_expanded_attachments(data)
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def get_requirements_by_query(
    filter_id: str = "",
    search: str = "",
    filters: JsonStrMap | None = None,
    fields: JsonStrList | None = None,
    page: int = 1,
    per_page: int = 300,
    formatted_text: bool = True,
    expand: JsonStrList | None = None,
) -> str:
    """
    Search requirements in the active project via a POST query body. Wraps
    POST /{projectID}/requirements/search. Handles both filtered searches and
    unfiltered listings; called with no args it returns the first page only (up
    to `per_page`, default 300). Paginate via `page`/`per_page` (see
    `paging.totalPages`/`totalCount` in the response) to retrieve them all. Sends
    parameters in a JSON body, avoiding URL-length and URL-encoding limits.

    Args:
        filter_id:      Saved-filter name or ID to apply.
        search:         Free-form Perforce ALM search expression. Field names are
                        labels that must match an existing field; matching is
                        case-insensitive (a nonexistent field 404s). String
                        literals use single or double quotes; there is no escape
                        for a quote inside a literal, so wrap a value containing '
                        in double quotes and a value containing " in single quotes.
                        Operators include =, !=, contains, and, or, plus parens.
                        Example: "Description contains 'WysiCorp'".
        filters:        Shorthand for equality-only filtering. Each key is a
                        field label — matched case-insensitively, but it must
                        name an existing field (multi-word labels like
                        "Multi Word Field" are supported — the shorthand
                        double-quotes labels automatically); each value is
                        matched as a string literal (quoted with whichever quote
                        preserves any literal quote in the value; a value with
                        both ' and " cannot be expressed and errors). Combined
                        with `and`.
                        If `search` is also supplied, the two are `and`-joined.
                        Example: {"Product": "Perforce ALM"}.
        fields:         Field labels to return on each item (case-insensitive,
                        e.g. "Summary", "Product"). Top-level props (id, number,
                        tag) are always returned and don't need listing.
        page:           Page number (default 1, min 1).
        per_page:       Items per page (default 300, max 1000).
        formatted_text: Return multi-line text as HTML (True) or plain text (False).
        expand:         Sub-objects to expand. Allowed values: attachments,
                        events, documents, versions, links, folders.

    Response: {
      "requirements": [<Requirement>, ...],  # each item: see get_requirement
      "paging": {"page": <int>, "pageLimit": <int>,
                 "totalPages": <int|null>, "totalCount": <int|null>}
    }
    Chain requirement `id` values into `update_requirements`, `get_requirement`,
    or `add_document_tree_nodes` (as `requirement_ids`). Each item is shaped like `get_requirement`'s
    response; `fields` reflects what was requested via the `fields` param, and
    the expand-gated keys (attachments, events, documents, versions, links,
    folders) appear only when listed in `expand`.
    """
    body = _build_search_body(
        filter_id=filter_id, search=search, filters=filters, fields=fields,
        page=page, per_page=per_page, formatted_text=formatted_text, expand=expand,
    )

    path = _project_path("/requirements/search")

    try:
        data = _token_authenticated_request("POST", path, body=body)
    except Exception as e:
        return f"Error: {e}"
    for item in data.get("requirements", []):
        _strip_expanded_attachments(item)
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_WRITE_ADDITIVE)
def create_requirements(
    fields: list[dict],
    requirement_type: JsonObj | None = None,
    folders: JsonObjList | None = None,
) -> str:
    """
    Create a requirement in the active project. Wraps POST /{projectID}/requirements.

    To create multiple requirements, call this tool once per requirement rather than
    batching — per-call responses make it easier to surface server-assigned IDs and
    isolate validation errors.

    Server-assigned values (id, number, tag, ttstudioURL, httpURL, events, versions,
    documents) are populated by Perforce ALM and should not be supplied.

    Args:
        fields:           List of field dicts. Identify each field by EITHER its
                          integer `id` OR its string `label` (the field's display
                          name) — put the name in `label`, never in `id`. Each dict
                          also needs `type` (one of: string, formattedString,
                          menuItem, menuItemArray, editableVersion, boolean, integer,
                          decimal, date, dateTime, user, userArray) and the matching type-specific
                          value key. Examples:
                            by id:    {"id": 5, "type": "string", "string": "Login form"}
                            by label: {"label": "Summary", "type": "string", "string": "Login form"}
                            menu:     {"label": "Product", "type": "menuItem", "menuItem": {"id": 132}}
        requirement_type: {"id": int} or {"label": str} identifying the requirement
                          type. Required when creating — every requirement must have
                          a type, and the REST API does not apply a project default.
        folders:          Folder placements as a list, e.g. [{"id": 135}] or
                          [{"path": "/Public/Product APS/v7.5 Release"}]. The tool
                          wraps this in {"foldersData": ...}.

    Note: if `folders` is supplied, folder placement is NOT atomic with the
    create. An invalid folder path/ID doesn't fail the whole request — the
    requirement is still created, and every valid folder entry in the list
    is applied; an invalid entry is simply skipped and reported in `errors`,
    it does not block other entries (including ones later in the list) from
    being applied. When the REST API reports this as a 206 partial success
    (or any response with a non-empty `errors` array), this tool prepends a
    top-level "warning" key to the JSON response — re-fetch the created
    requirement with expand=["folders"] rather than assuming the requested
    folders were applied.

    Links cannot be created here: the requirements create endpoint silently
    discards any link payload (returns success but creates no link — verified
    empirically), so there is intentionally no `links` argument. Add links
    after the requirement exists with create_requirement_links.

    Attachments cannot be set at creation either: this tool exposes no
    attachments argument, because the REST create endpoint silently discards
    any attachmentsData payload the same way — it returns success but attaches
    nothing. Add attachments after the requirement exists with
    upload_requirement_attachment.

    Response: {
      "requirements": [
        {"id": <int>, "number": <int>, "tag": <str>,
         "self": <str>, "ttstudioURL": <str>, "httpURL": <str>,
         "requirementType": {"id": <int>, "label": <str>}}
      ],
      "errors": [  # only present on 206 partial-success
        {"code": <str>, "message": <str>, "statusCode": <int>,
         "errorElementPath": <str>}, ...
      ],
      "warning": <str>  # only present on a 206/partial-success response that touched `folders` — see the Note above
    }
    The `requirements` array always has length 1 (this tool creates one per
    call, even though the REST endpoint is bulk). Chain the returned `id`
    into get_requirement or update_requirements. `errors` is only present on
    a 206 partial-success response — most often from an invalid `folders`
    entry (see the Note above) — but if you see it, inspect
    `errorElementPath` to locate the cause. Note: the spec's response schema
    is `ItemBaseArray` (no `requirementType`), but the server's actual
    response includes it — verified empirically; treat as
    implementation-side enrichment that may change.
    """
    requirement: dict = {"fields": fields}
    if requirement_type is not None:
        requirement["requirementType"] = requirement_type
    if folders is not None:
        requirement["folders"] = {"foldersData": folders}

    body = {"requirements": [requirement]}
    path = _project_path("/requirements")

    try:
        data, status = _token_authenticated_request_with_status("POST", path, body)
    except Exception as e:
        return f"Error: {e}"
    warning = _folders_partial_success_warning(status, data, folders is not None, is_create=True)
    if warning:
        data = {"warning": warning, **data}
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_WRITE_DESTRUCTIVE)
def update_requirements(requirements: list[dict]) -> str:
    """
    Update one or more requirements in the active project. Wraps
    PUT /{projectID}/requirements. Pass a length-1 list to update a single item.

    Each dict must include `id` plus any fields/sub-objects to change.
    Sub-objects are supplied unwrapped — this tool wraps them into their
    envelopes ({"foldersData": [...]}, etc.) before sending.

    Note: if `folders` is supplied for an item, it REPLACES that requirement's
    entire folder-placement collection wholesale (server-side behavior of the
    PUT endpoint) — passing an empty list removes the requirement from every
    folder it is currently in, filing it nowhere. Omit `folders` from the
    payload to leave existing placements untouched.

    Warning: that folders replace is NOT atomic. If the folders list contains
    an invalid path or ID, the request can still change the requirement's
    folder placements before reporting the error: the server removes the
    existing placements first, then applies every valid entry in the list —
    an invalid entry is simply skipped and reported in `errors`, it does not
    block other entries (including ones later in the list) from being
    applied — so a failed call can leave the requirement filed in some, all,
    or none of the requested folders, with its original folders already
    gone regardless. When the REST API reports this as a 206 partial success
    (or any response with a non-empty `errors` array) for a request that
    touched `folders`, this tool prepends a top-level "warning" key to the
    JSON response — re-fetch the affected requirement(s) with
    expand=["folders"] rather than assuming nothing changed.

    Links are not editable through this tool. Manage requirement links with the
    dedicated link tools: create_requirement_links (add) and
    list_requirement_links (read).

    Attachments cannot be changed with a PUT and are rejected here: the REST
    update endpoint silently discards an attachments payload (returns success
    but changes nothing). upload_requirement_attachment is the only way to add
    one; there is no endpoint to edit or remove an existing attachment.

    Server-managed values (number, tag, ttstudioURL, httpURL, events, versions,
    documents) should not be supplied.

    Args:
        requirements: List of requirement dicts. Per-item dict keys:
            id (required):   Record ID of the requirement to update.
            fields:          List of field dicts. Same shape as create_requirements:
                             identify each field by EITHER `id` (integer) OR `label`
                             (string field name, never placed in `id`), plus `type`
                             and the matching type-specific value key.
            requirementType: {"id": int} or {"label": str}.
            folders:         Folder placements as a list, e.g. [{"id": 135}] or
                             [{"path": "/Public/Product APS/v7.5 Release"}]. Tool
                             wraps this in {"foldersData": ...}. Replaces ALL
                             existing folder placements.

    Response: {
      "warning": <str>,  # only present on a 206/partial-success response that touched `folders` — see the Warning above
      "requirements": [
        {"id": <int>, "number": <int>, "tag": <str>,
         "self": <str>, "ttstudioURL": <str>, "httpURL": <str>,
         "requirementType": {"id": <int>, "label": <str>}},
        ...
      ],
      "errors": [  # only present on 206 partial-success
        {"code": <str>, "message": <str>, "statusCode": <int>,
         "errorElementPath": <str>}, ...
      ]
    }
    The `requirements` array has one entry per successful item. On 206
    partial-success, some items in the batch updated and others didn't —
    inspect each error's `errorElementPath` (e.g. "/requirements/2") to
    locate the failing input by index. Note: the spec's response schema is
    `ItemBaseArray` (no `requirementType`), but the server's actual response
    includes it — verified empirically; treat as implementation-side
    enrichment that may change.
    """
    allowed = {"id", "fields", "requirementType", "folders"}
    not_updatable = {"links", "events", "attachments"}
    not_updatable_reasons = {
        "links": "links do not have an editing path. Add them with create_requirement_links instead of update_requirements.",
        "events": "events cannot be updated with PUT.",
        "attachments": "attachments cannot be updated with PUT. Add them with upload_requirement_attachment.",
    }
    wrapped: list[dict] = []
    for i, req in enumerate(requirements):
        if "id" not in req:
            return f"Error: requirements[{i}] missing the required 'id' key."
        blocked = not_updatable & set(req)
        if blocked:
            reasons = "; ".join(not_updatable_reasons[k] for k in sorted(blocked))
            return f"Error: requirements[{i}] cannot include {sorted(blocked)} in an update: {reasons}"
        unknown = set(req) - allowed - not_updatable
        if unknown:
            return f"Error: requirements[{i}] has unknown keys: {sorted(unknown)}"
        item: dict = {"id": req["id"]}
        if "fields" in req:
            item["fields"] = req["fields"]
        if "requirementType" in req:
            item["requirementType"] = req["requirementType"]
        if "folders" in req:
            item["folders"] = {"foldersData": req["folders"]}
        wrapped.append(item)

    body = {"requirements": wrapped}
    path = _project_path("/requirements")

    try:
        data, status = _token_authenticated_request_with_status("PUT", path, body)
    except Exception as e:
        return f"Error: {e}"
    warning = _folders_partial_success_warning(status, data, any("folders" in item for item in wrapped))
    if warning:
        data = {"warning": warning, **data}
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def list_requirement_links(item_id: int) -> str:
    """
    Get the links attached to a requirement. Wraps
    GET /{projectID}/requirements/{itemID}/links.

    Useful for discovering the exact shape of a project-configured link
    definition (e.g. "Verified by") before writing a create call: add the link
    by hand in the UI on one requirement, fetch it with this tool, and copy the
    `linkDefinition.id`, `type`, and relationship structure into your
    create_requirement_links payload.

    Args:
        item_id: Record ID of the requirement.

    Response: {
      "self": <str>,                # REST href to /requirements/{id}/links
      "linksData": [
        {
          "id": <int>,              # link record ID
          "comment": <str>,
          "linkDefinition": {"id": <int>, "name": <str>},
          "type": "peers" | "parentChildren",   # discriminator below
          "peers": [<LinkedItem>, ...],          # present when type == "peers"
          "parentChildren": {                    # present when type == "parentChildren"
            "parent": <LinkedItem>,
            "children": [<LinkedItem>, ...]
          }
        }, ...
      ]
    }
    Each LinkedItem: {"itemID": <int>, "itemType": "issues"|"testCases"|
    "testRuns"|"requirements"|"documents"|"automatedTestResults",
    "isSuspect": <bool>, "link": <str>}. The `linkDefinition.id` and `type`
    fields are what you'll need when constructing payloads for
    create_requirement_links.
    """
    path = _project_path(f"/requirements/{item_id}/links")

    try:
        data = _token_authenticated_request("GET", path)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_WRITE_ADDITIVE)
def create_requirement_links(item_id: int, links: list[dict]) -> str:
    """
    Add one or more links to a requirement. Wraps
    POST /{projectID}/requirements/{itemID}/links.

    This is the supported path for managing requirement links: the requirements
    create endpoint ignores inline link payloads (see create_requirements), so
    links can't be set at creation and must be added here, after the
    requirement exists (e.g. linking a requirement to the test cases that
    verify it, or to a related requirement, document, or issue).

    Args:
        item_id: Record ID of the requirement.
        links:   List of Link objects. Each needs `linkDefinition` ({"id": ...}
                 or {"name": ...}), `type` ("peers" or "parentChildren"), and
                 the matching relationship key — `peers` (LinkedItem array) for
                 peer-type links, or `parentChildren` ({"parent": LinkedItem,
                 "children": [LinkedItem, ...]}) for hierarchical links. Each
                 LinkedItem is {"itemID": <int>, "itemType": "issues" |
                 "testCases" | "testRuns" | "requirements" | "documents" |
                 "automatedTestResults"}. Each link may also carry an optional
                 `comment` (string) annotating the link. The tool wraps the list
                 in {"linksData": ...}. For `peers`-type links, `peers` must
                 include a LinkedItem for this requirement itself alongside the
                 other side(s) — omitting it silently no-ops: the call returns
                 success with an empty `linksData`, creating nothing.

    Response (201 success): {
      "linksData": [<Link>, ...]   # full Link shape — see list_requirement_links
    }
    Response (206 partial success): {
      "linksData": [<Link>, ...],
      "errors": [
        {"code": <str>, "message": <str>, "statusCode": <int>,
         "errorElementPath": <str>}, ...   # e.g. "/linksData/2"
      ]
    }
    The response echoes the full Link objects (including the server-assigned
    `id`), so no follow-up GET is needed to read back the created links.
    """
    body = {"linksData": links}
    path = _project_path(f"/requirements/{item_id}/links")

    try:
        data = _token_authenticated_request("POST", path, body=body)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def list_requirement_attachments(item_id: int) -> str:
    """
    Get metadata for the files attached to a requirement. Wraps
    GET /{projectID}/requirements/{itemID}/attachments. Returns metadata only,
    not file contents.

    Args:
        item_id: Record ID of the requirement.

    Response: {
      "self": <str>,   # REST href to /requirements/{id}/attachments
      "attachmentsData": [
        {"id": <int>, "filename": <str>, "size": <int>,
         "created": <str|null>, "modified": <str|null>,
         "encodedFileID": <str>}, ...
      ]
    }
    `encodedFileID` identifies the file for download_attachment; `id` is the
    attachment record ID.
    """
    path = _project_path(f"/requirements/{item_id}/attachments")
    try:
        data = _token_authenticated_request("GET", path)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(_strip_attachment_content(data), indent=2)


@mcp.tool(annotations=_WRITE_ADDITIVE)
def upload_requirement_attachment(item_id: int, file_path: str) -> str:
    """
    Attach a local file to a requirement. Wraps
    POST /{projectID}/requirements/{itemID}/attachments (multipart/form-data).

    Args:
        item_id:   Record ID of the requirement.
        file_path: Path to a file on the machine running THIS MCP server (not
                   the caller's machine), confined to the server's configured
                   upload directory (PERFORCE_ALM_MCP_UPLOAD_DIR; defaults to
                   a folder under the system temp directory) — a path that
                   resolves outside it is rejected. The file's bytes are
                   uploaded and its base name is preserved as the attachment
                   filename.

    Response: {
      "attachmentsData": [
        {"id": <int>, "filename": <str>, "encodedFileID": <str>}
      ]
    }
    The returned `id` is the new attachment record ID; `encodedFileID`
    identifies the file for download_attachment. A missing/unreadable
    file_path, or one outside the upload root, returns an "Error:" string.
    """
    path = _project_path(f"/requirements/{item_id}/attachments")
    try:
        data = _upload_attachment_request(path, file_path)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(_strip_attachment_content(data), indent=2)


# Field record IDs for the issue fields that are actually read-only mirrors of
# foundByRecords[0] (see the Found-by mirror fields note in CLAUDE.md), mapped
# to the found_by_records/foundByRecords key that actually controls them.
# These are fixed "inherent field" IDs (Perforce ALM reserves ids 0-199 for
# built-in fields, universally, across every project) rather than per-project
# row IDs, so — unlike a label — an id here can't be changed by a project
# renaming the field. Verified against a live project's GET /issues response:
# Description=54, Date Found=8, Version Found=12,
# Steps to Reproduce=58, Reproducible=11, Test Config=13,
# Other Hardware and Software=64, Found By=9.
_ISSUE_FOUND_BY_MIRROR_FIELD_IDS = {
    54: "description",
    8: "dateFound",
    12: "versionFound",
    58: "steps",
    11: "reproduced",
    13: "testConfig",
    64: "otherConfig",
    9: "foundBy",
}


def _decorate_found_by_mirror_fields(fields: list[dict]) -> None:
    """Tag every `fields` entry that mirrors a found-by record property with
    `foundByRecordKey`, the found_by_records/foundByRecords key that actually
    controls it (see create_issues' found_by_records docs). Mutates `fields`
    in place. Used by get_issue/get_issues_by_query only — create_issues and
    update_issues rely on their docstrings, not runtime enforcement, to steer
    callers away from writing these labels via `fields`."""
    for field in fields:
        key = _ISSUE_FOUND_BY_MIRROR_FIELD_IDS.get(field.get("id"))
        if key:
            field["foundByRecordKey"] = key


@mcp.tool(annotations=_READ_ONLY)
def get_issue(
    item_id: int,
    fields: JsonStrList | None = None,
    formatted_text: bool = True,
    expand: JsonStrList | None = None,
) -> str:
    """
    Get a single issue by item ID. Wraps GET /{projectID}/issues/{itemID}.

    Args:
        item_id:        Record ID of the issue.
        fields:         Field labels to return (case-insensitive, e.g. "Summary",
                        "Status"). Top-level props (id, number, tag) are always
                        returned and don't need listing.
        formatted_text: Return multi-line text as HTML (True) or plain text (False).
        expand:         Sub-objects to expand. Allowed values: foundByRecords,
                        attachments, events, links, folders.

    Response: {
      "id": <int>, "number": <int>, "tag": <str>,
      "self": <str>, "ttstudioURL": <str>, "httpURL": <str>,
      "fields": [{"id": <int>, "label": <str>, "type": <str>, "<type>": <value>}, ...],
      # each field's value sits under a key named by its `type`, one of: string,
      # formattedString, menuItem, menuItemArray, editableVersion, boolean,
      # integer, decimal, date, dateTime, user, userArray
      # (e.g. {"id": 2, "label": "Summary", "type": "string", "string": "Login fails"})
      # expand-gated, each present only when listed in `expand`:
      "foundByRecords": <obj>, "attachments": <obj>, "events": <obj>,
      "links": <obj>, "folders": <obj>
    }
    Several `fields` entries (Description, Date Found, Version Found, Steps to
    Reproduce, Reproducible, Test Config, Other Hardware and Software, Found By)
    are read-only mirrors of the issue's first found-by record (visible under `foundByRecords`
    when expanded) — see create_issues `found_by_records` for the fixed-key list
    and why they must be set there, not via `fields`. Each such entry is tagged
    with an extra `foundByRecordKey` (e.g. `"foundByRecordKey": "description"`),
    naming the found_by_records/foundByRecords key that actually controls it —
    a quick way to spot these without memorizing the label list.
    Chain `id` into create_issues follow-ups or get_issues_by_query (search by it).
    """
    params: list[tuple[str, str]] = []
    if fields:
        params.extend(("fields", f) for f in fields)
    if not formatted_text:
        params.append(("formattedText", "false"))
    if expand:
        params.extend(("expand", e) for e in expand)

    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    path = _project_path(f"/issues/{item_id}{query}")

    try:
        data = _token_authenticated_request("GET", path)
    except Exception as e:
        return f"Error: {e}"
    if "fields" in data:
        _decorate_found_by_mirror_fields(data["fields"])
    _strip_issue_expanded_attachments(data)
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def get_issues_by_query(
    filter_id: str = "",
    search: str = "",
    filters: JsonStrMap | None = None,
    fields: JsonStrList | None = None,
    page: int = 1,
    per_page: int = 300,
    formatted_text: bool = True,
    expand: JsonStrList | None = None,
) -> str:
    """
    Search issues in the active project via a POST query body. Wraps
    POST /{projectID}/issues/search. Handles both filtered searches and
    unfiltered listings; called with no args it returns the first page only (up
    to `per_page`, default 300). Paginate via `page`/`per_page` (see
    `paging.totalPages`/`totalCount` in the response) to retrieve them all. Sends
    parameters in a JSON body, avoiding URL-length and URL-encoding limits.

    Args:
        filter_id:      Saved-filter name or ID to apply (e.g. "Assigned to Me").
        search:         Free-form Perforce ALM search expression. Field names are
                        labels that must match an existing field; matching is
                        case-insensitive (a nonexistent field 404s). String
                        literals use single or double quotes; there is no escape
                        for a quote inside a literal, so wrap a value containing '
                        in double quotes and a value containing " in single quotes.
                        Operators include =, !=, contains, and, or, plus parens.
                        Example: "Description contains 'WysiCorp'".
        filters:        Shorthand for equality-only filtering. Each key is a
                        field label — matched case-insensitively, but it must
                        name an existing field (multi-word labels like
                        "Multi Word Field" are supported — the shorthand
                        double-quotes labels automatically); each value is
                        matched as a string literal (quoted with whichever quote
                        preserves any literal quote in the value; a value with
                        both ' and " cannot be expressed and errors). Combined
                        with `and`.
                        If `search` is also supplied, the two are `and`-joined.
                        Example: {"Status": "Open"}.
        fields:         Field labels to return on each item (case-insensitive,
                        e.g. "Summary", "Status"). Top-level props (id, number,
                        tag) are always returned and don't need listing.
        page:           Page number (default 1, min 1).
        per_page:       Items per page (default 300, max 1000).
        formatted_text: Return multi-line text as HTML (True) or plain text (False).
        expand:         Sub-objects to expand. Allowed values: foundByRecords,
                        attachments, events, links, folders.

    Response: {
      "issues": [<Issue>, ...],
      "paging": {"page": <int>, "pageLimit": <int>,
                 "totalPages": <int|null>, "totalCount": <int|null>}
    }
    Each issue always carries top-level `id` (integer), `number`, and `tag`; the
    requested `fields` are returned per item, and the expand-gated keys
    (foundByRecords, attachments, events, links, folders) appear only when listed
    in `expand`. Extract `id` from each item to chain into follow-up issue tools.
    Several `fields` entries (Description, Date Found, Version Found, Steps to
    Reproduce, Reproducible, Test Config, Other Hardware and Software, Found By)
    are read-only mirrors of each issue's first found-by record — see create_issues
    `found_by_records` for the fixed-key list and why they must be set there,
    not via `fields`. Each such entry is tagged with an extra `foundByRecordKey`
    (e.g. `"foundByRecordKey": "description"`), naming the found_by_records/
    foundByRecords key that actually controls it — a quick way to spot these
    without memorizing the label list.
    """
    body = _build_search_body(
        filter_id=filter_id, search=search, filters=filters, fields=fields,
        page=page, per_page=per_page, formatted_text=formatted_text, expand=expand,
    )

    path = _project_path("/issues/search")

    try:
        data = _token_authenticated_request("POST", path, body=body)
    except Exception as e:
        return f"Error: {e}"
    for issue in data.get("issues", []):
        if "fields" in issue:
            _decorate_found_by_mirror_fields(issue["fields"])
        _strip_issue_expanded_attachments(issue)
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_WRITE_ADDITIVE)
def create_issues(
    fields: list[dict],
    found_by_records: JsonObjList | None = None,
    folders: JsonObjList | None = None,
) -> str:
    """
    Create an issue in the active project. Wraps POST /{projectID}/issues.

    To create multiple issues, call this tool once per issue rather than
    batching — per-call responses make it easier to surface server-assigned IDs
    and isolate validation errors.

    Server-assigned values (id, number, tag, ttstudioURL, httpURL, events) are
    populated by Perforce ALM and should not be supplied.

    Args:
        fields:           List of field dicts. Identify each field by EITHER its
                          integer `id` OR its string `label` (the field's display
                          name) — put the name in `label`, never in `id`. Provide
                          the value under the key matching the field type (string,
                          formattedString, menuItem, menuItemArray, editableVersion,
                          boolean, integer, decimal, date, dateTime, user, userArray);
                          an explicit `type` key is optional (the value key implies
                          it) but may be included for clarity. Examples:
                            string: {"label": "Summary", "string": "Login form fails"}
                            menu:   {"label": "Type", "menuItem": {"label": "Question"}}
                            menu:   {"label": "Product",
                                     "menuItem": {"label": "Activity Professional Suite (APS)"}}
        found_by_records: "Found by" records as a list of FoundByRecord dicts (issue-
                          specific). Omit `id` on each to add a new record. Each
                          dict may include any of these fixed keys:
                              dateFound:    string, "YYYY-MM-DD", e.g. "2018-09-15"
                              versionFound: string, e.g. "1.0"
                              description:  TextField: {"text": "...", "isFormatted": bool}
                              reproduced:   MenuItem: {"id": <int>} or {"label": "..."}
                              steps:        TextField: {"text": "...", "isFormatted": bool}
                              testConfig:   MenuItem: {"id": <int>} or {"label": "..."}
                              otherConfig:  TextField: {"text": "...", "isFormatted": bool}
                              foundBy:      User: {"username": "..."} or
                                            {"firstName": "...", "lastName": "..."}
                          Complete example:
                              {"dateFound": "2026-07-15", "versionFound": "1.0",
                               "description": {"text": "Login fails after reset",
                                                "isFormatted": false},
                               "reproduced": {"label": "Always"},
                               "steps": {"text": "1. Reset password\n2. Log in",
                                         "isFormatted": false},
                               "testConfig": {"label": "Windows 11 / Chrome"},
                               "otherConfig": {"text": "Also fails on staging",
                                               "isFormatted": false},
                               "foundBy": {"username": "jsmith"}}
                          See the note below on mirrored fields for why these must
                          be set here rather than in `fields`. The tool wraps this
                          in {"foundByRecordsData": ...}.
        folders:          Folder placements as a list, e.g. [{"id": 135}] or
                          [{"path": "/Public/Product APS/v7.5 Release"}]. The tool
                          wraps this in {"foldersData": ...}.

    Note: if `folders` is supplied, folder placement is NOT atomic with the
    create. An invalid folder path/ID doesn't fail the whole request — the
    issue is still created, and every valid folder entry in the list is
    applied; an invalid entry is simply skipped and reported in `errors`, it
    does not block other entries (including ones later in the list) from
    being applied. When the REST API reports this as a 206 partial success
    (or any response with a non-empty `errors` array), this tool prepends a
    top-level "warning" key to the JSON response — re-fetch the created
    issue with expand=["folders"] rather than assuming the requested folders were
    applied.

    Several fields shown in an issue's generic `fields` array are actually read-only
    mirrors of the issue's first found-by record — writing to them via `fields` is
    silently ignored (the call succeeds, but the value never changes). Known
    mirrored pairs (found_by_records key -> default fields-array label): dateFound ->
    "Date Found", versionFound -> "Version Found", description -> "Description",
    reproduced -> "Reproducible", steps -> "Steps to Reproduce", testConfig -> "Test
    Config", otherConfig -> "Other Hardware and Software", foundBy -> "Found By".
    To actually set
    any of these, put it in `found_by_records` under its fixed key above — never
    under its fields-array label. If a `fields` write to one of these doesn't seem
    to take effect after a get_issue, this mirroring is why. Using these fixed keys
    also sidesteps the field-rename limitation noted in README's "Known
    limitations": unlike `fields`-array labels (which break if a project admin
    renames the field), found_by_records keys are fixed JSON schema slots that keep
    working regardless of any display-label rename.

    Links cannot be set at creation: create_issues exposes no link argument, and
    the REST create (POST) endpoint silently ignores an inline link payload anyway
    — it returns success but attaches no link (same behavior as requirements and
    test cases). Add links after the issue exists with create_issue_links.

    Attachments cannot be set at creation either: create_issues exposes no
    attachments argument, because the REST create endpoint silently discards any
    attachmentsData payload (top-level or nested under a found-by record) — it
    returns success but attaches nothing. Add attachments after the issue (and
    its found-by record) exist, via upload_issue_found_by_attachment; use
    list_issue_attachments to read existing attachments.

    Response: {
      "issues": [
        {"id": <int>, "number": <int>, "tag": <str>,
         "self": <str>, "ttstudioURL": <str>, "httpURL": <str>}
      ],
      "errors": [  # only present on 206 partial-success
        {"code": <str>, "message": <str>, "statusCode": <int>,
         "errorElementPath": <str>}, ...
      ],
      "warning": <str>  # only present on a 206/partial-success response that touched `folders` — see the Note above
    }
    The `issues` array always has length 1 (this tool creates one per call, even
    though the REST endpoint is bulk). Chain the returned `id` into
    get_issues_by_query (search by it) or future issue tools. `errors` is only
    present on a 206 partial-success response — most often from an invalid
    `folders` entry (see the Note above) — but if you see it, inspect
    `errorElementPath` to locate the cause.
    """
    issue: dict = {"fields": fields}
    if found_by_records is not None:
        issue["foundByRecords"] = {"foundByRecordsData": found_by_records}
    if folders is not None:
        issue["folders"] = {"foldersData": folders}

    body = {"issues": [issue]}
    path = _project_path("/issues")

    try:
        data, status = _token_authenticated_request_with_status("POST", path, body)
    except Exception as e:
        return f"Error: {e}"
    warning = _folders_partial_success_warning(status, data, folders is not None, is_create=True)
    if warning:
        data = {"warning": warning, **data}
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_WRITE_DESTRUCTIVE_UNSAFE_RETRY)
def update_issues(issues: list[dict]) -> str:
    """
    Update one or more issues in the active project. Wraps
    PUT /{projectID}/issues. Pass a length-1 list to update a single item.

    Each dict must include `id` plus any fields/sub-objects to change.
    Sub-objects are supplied unwrapped — this tool wraps them into their
    envelopes ({"foldersData": [...]}, {"foundByRecordsData": [...]}) before
    sending. (`id` isn't marked required by the REST schema, but a PUT needs it
    to identify the target issue, so this tool requires it.)

    Note: if `folders` is supplied for an item, it REPLACES that issue's
    entire folder-placement collection wholesale (server-side behavior of the
    PUT endpoint) — passing an empty list removes the issue from every folder
    it is currently in, filing it nowhere. Omit `folders` from the payload to
    leave existing placements untouched.

    Warning: that folders replace is NOT atomic. If the folders list contains
    an invalid path or ID, the request can still change the issue's folder
    placements before reporting the error: the server removes the existing
    placements first, then applies every valid entry in the list — an
    invalid entry is simply skipped and reported in `errors`, it does not
    block other entries (including ones later in the list) from being
    applied — so a failed call can leave the issue filed in some, all, or
    none of the requested folders, with its original folders already gone
    regardless. When the REST API reports this as a 206 partial success (or
    any response with a non-empty `errors` array) for a request that touched
    `folders`, this tool prepends a top-level "warning" key to the JSON
    response — re-fetch the affected issue(s) with expand=["folders"] rather
    than assuming nothing changed.

    Attachments and events CANNOT be changed with a PUT and are rejected here.
    Add attachments with upload_issue_found_by_attachment instead. Links are also
    rejected: add them with create_issue_links (editing or removing existing
    links is intentionally unsupported, matching requirements and test cases).

    Several display fields on an issue (Description, Date Found, Version Found,
    Steps to Reproduce, Reproducible, Test Config, Other Hardware and Software,
    Found By) are read-only mirrors of the found-by record and cannot be set via `fields` —
    writes there are silently ignored. Set them via `foundByRecords` instead,
    using its fixed keys (dateFound, versionFound, description, reproduced, steps,
    testConfig, otherConfig, foundBy). See create_issues `found_by_records` for the
    full FoundByRecord shape. Using these fixed keys also sidesteps the
    field-rename limitation that applies to `fields`-array labels (see README's
    "Known limitations").

    Server-managed values (number, tag, ttstudioURL, httpURL) should not be
    supplied.

    Args:
        issues: List of issue dicts. Per-item dict keys:
            id (required):  Record ID of the issue to update.
            fields:         List of field dicts. Same shape as create_issues:
                            identify each field by EITHER `id` (integer) OR
                            `label` (string field name, never placed in `id`),
                            with the value under the type-matching key (string,
                            menuItem, ...).
            folders:        Folder placements as a list, e.g. [{"id": 135}] or
                            [{"path": "/Public/Product APS/v7.5 Release"}]. Tool
                            wraps this in {"foldersData": ...}. Replaces ALL
                            existing folder placements.
            foundByRecords: "Found by" records (issue-specific). CAUTION — the
                            list you send REPLACES the issue's entire Found-by
                            collection wholesale:
                              * Omit `foundByRecords` entirely to leave the
                                existing records untouched.
                              * To ADD a record, include it WITHOUT an `id`.
                              * To UPDATE existing records, include their `id`s —
                                and include the `id` of every OTHER record you
                                want to keep. Any existing record whose `id` is
                                not in the list is DELETED from the issue —
                                including any attachments uploaded to that record
                                via upload_issue_found_by_attachment, which are
                                deleted along with it.
                              * Array order is the display order (arrange to
                                reorder).
                            The tool wraps this in {"foundByRecordsData": ...}.

    Response: {
      "warning": <str>,  # only present on a 206/partial-success response that touched `folders` — see the Warning above
      "issues": [
        {"id": <int>, "number": <int>, "tag": <str>,
         "self": <str>, "ttstudioURL": <str>, "httpURL": <str>},
        ...
      ],
      "errors": [  # only present on 206 partial-success
        {"code": <str>, "message": <str>, "statusCode": <int>,
         "errorElementPath": <str>}, ...   # e.g. "/issues/0/fields/1/label"
      ]
    }
    The `issues` array has one entry per successful item. On 206 partial-success,
    some items updated and others didn't — inspect each error's `errorElementPath`
    to locate the failing input by index.
    """
    allowed = {"id", "fields", "folders", "foundByRecords"}
    # Known Issue sub-objects that a PUT cannot change: attachments and events
    # are read-only on update per the REST API; links have no editing path (see
    # the link-tool footgun notes elsewhere). Rejected explicitly so the caller
    # gets a targeted message instead of a generic "unknown keys".
    not_updatable = {"attachments", "events", "links"}
    not_updatable_reasons = {
        "attachments": "attachments cannot be updated with PUT. Add them with upload_issue_found_by_attachment.",
        "events": "events cannot be updated with PUT.",
        "links": "links do not have an editing path. Add them with create_issue_links instead of update_issues.",
    }
    wrapped: list[dict] = []
    for i, issue in enumerate(issues):
        if "id" not in issue:
            return f"Error: issues[{i}] missing the required 'id' key."
        blocked = not_updatable & set(issue)
        if blocked:
            reasons = "; ".join(not_updatable_reasons[k] for k in sorted(blocked))
            return f"Error: issues[{i}] cannot include {sorted(blocked)} in an update: {reasons}"
        unknown = set(issue) - allowed - not_updatable
        if unknown:
            return f"Error: issues[{i}] has unknown keys: {sorted(unknown)}"
        item: dict = {"id": issue["id"]}
        if "fields" in issue:
            item["fields"] = issue["fields"]
        if "folders" in issue:
            item["folders"] = {"foldersData": issue["folders"]}
        if "foundByRecords" in issue:
            item["foundByRecords"] = {"foundByRecordsData": issue["foundByRecords"]}
        wrapped.append(item)

    body = {"issues": wrapped}
    path = _project_path("/issues")

    try:
        data, status = _token_authenticated_request_with_status("PUT", path, body)
    except Exception as e:
        return f"Error: {e}"
    warning = _folders_partial_success_warning(status, data, any("folders" in item for item in wrapped))
    if warning:
        data = {"warning": warning, **data}
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def list_issue_links(item_id: int) -> str:
    """
    Get the links attached to an issue. Wraps
    GET /{projectID}/issues/{itemID}/links.

    Useful for discovering the exact shape of a project-configured link
    definition before writing a create call: add the link by hand in the UI on
    one issue, fetch it with this tool, and copy the `linkDefinition.id`, `type`,
    and relationship structure into your create_issue_links payload.

    Args:
        item_id: Record ID of the issue.

    Response: {
      "self": <str>,                # REST href to /issues/{id}/links
      "linksData": [
        {
          "id": <int>,              # link record ID
          "comment": <str>,
          "linkDefinition": {"id": <int>, "name": <str>},
          "type": "peers" | "parentChildren",   # discriminator below
          "peers": [<LinkedItem>, ...],          # present when type == "peers"
          "parentChildren": {                    # present when type == "parentChildren"
            "parent": <LinkedItem>,
            "children": [<LinkedItem>, ...]
          }
        }, ...
      ]
    }
    Each LinkedItem: {"itemID": <int>, "itemType": "issues"|"testCases"|
    "testRuns"|"requirements"|"documents"|"automatedTestResults",
    "isSuspect": <bool>, "link": <str>}. The `linkDefinition.id` and `type`
    fields are what you'll need when constructing payloads for create_issue_links.
    """
    path = _project_path(f"/issues/{item_id}/links")

    try:
        data = _token_authenticated_request("GET", path)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_WRITE_ADDITIVE)
def create_issue_links(item_id: int, links: list[dict]) -> str:
    """
    Add one or more links to an issue. Wraps
    POST /{projectID}/issues/{itemID}/links.

    This is the supported path for attaching issue links: create_issues exposes
    no link argument, so links are added here after the issue exists (e.g.
    linking an issue to the requirement it affects or the test case that
    reproduces it). Links are bidirectional, so an issue↔requirement or
    issue↔test-case link can equivalently be set from the other side.

    Args:
        item_id: Record ID of the issue.
        links:   List of Link objects. Each needs `linkDefinition` ({"id": ...}
                 or {"name": ...}), `type` ("peers" or "parentChildren"), and
                 the matching relationship key — `peers` (LinkedItem array) for
                 peer-type links, or `parentChildren` ({"parent": LinkedItem,
                 "children": [LinkedItem, ...]}) for hierarchical links. Each
                 LinkedItem is {"itemID": <int>, "itemType": "issues" |
                 "requirements" | "testCases" | "testRuns" | "documents" |
                 "automatedTestResults"}. Each link may also carry an optional
                 `comment` (string) annotating the link. The tool wraps the list
                 in {"linksData": ...}. For `peers`-type links, `peers` must
                 include a LinkedItem for this issue itself alongside the other
                 side(s) — omitting it silently no-ops: the call returns success
                 with an empty `linksData`, creating nothing.

    Response (201 success): {
      "linksData": [<Link>, ...]   # full Link shape — see list_issue_links
    }
    Response (206 partial success): {
      "linksData": [<Link>, ...],
      "errors": [
        {"code": <str>, "message": <str>, "statusCode": <int>,
         "errorElementPath": <str>}, ...   # e.g. "/linksData/2"
      ]
    }
    The response echoes the full Link objects (including the server-assigned
    `id`), so no follow-up GET is needed to read back the created links.
    """
    body = {"linksData": links}
    path = _project_path(f"/issues/{item_id}/links")

    try:
        data = _token_authenticated_request("POST", path, body=body)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def list_issue_attachments(item_id: int) -> str:
    """
    Get metadata for the files attached to an issue. Wraps
    GET /{projectID}/issues/{itemID}/attachments. Returns metadata only, not file
    contents.

    There is no issue-level attachment resource: issue attachments live on a
    "found by" record or a workflow event (event attachments aren't wrapped
    here), and this GET aggregates and lists all of them across the issue.
    Use upload_issue_found_by_attachment to add one.

    Args:
        item_id: Record ID of the issue.

    Response: {
      "self": <str>,   # REST href to /issues/{id}/attachments
      "attachmentsData": [
        {"id": <int>, "filename": <str>, "size": <int>,
         "created": <str|null>, "modified": <str|null>,
         "encodedFileID": <str>}, ...
      ]
    }
    `encodedFileID` identifies the file for download_attachment; `id` is the
    attachment record ID.
    """
    path = _project_path(f"/issues/{item_id}/attachments")
    try:
        data = _token_authenticated_request("GET", path)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(_strip_attachment_content(data), indent=2)


@mcp.tool(annotations=_WRITE_ADDITIVE)
def upload_issue_found_by_attachment(item_id: int, found_by_record_id: int, file_path: str) -> str:
    """
    Attach a local file to a "found by" record on an issue. Wraps
    POST /{projectID}/issues/{itemID}/foundByRecords/{foundByRecordID}/attachments
    (multipart/form-data).

    Issues have no item-level attachment-upload endpoint; attachments are
    uploaded to one of the issue's found-by records instead. list_issue_attachments
    reads back attachments from every found-by record on the issue, so there is
    no separate list-by-found-by-record tool.

    Args:
        item_id:             Record ID of the issue.
        found_by_record_id:  Record ID of the found-by record to attach the file
                             to. Discover existing IDs via get_issue(item_id,
                             expand=["foundByRecords"]) or add one first via
                             create_issues/update_issues's found_by_records
                             argument.
        file_path:           Path to a file on the machine running THIS MCP
                             server (not the caller's machine), confined to
                             the server's configured upload directory
                             (PERFORCE_ALM_MCP_UPLOAD_DIR; defaults to a
                             folder under the system temp directory) — a path
                             that resolves outside it is rejected. The file's
                             bytes are uploaded and its base name is preserved
                             as the attachment filename.

    Response: {
      "attachmentsData": [
        {"id": <int>, "filename": <str>, "encodedFileID": <str>}
      ]
    }
    The returned `id` is the new attachment record ID; `encodedFileID`
    identifies the file for download_attachment. A missing/unreadable
    file_path, a path outside the upload root, or a nonexistent
    item_id/found_by_record_id, returns an "Error:" string.
    """
    path = _project_path(f"/issues/{item_id}/foundByRecords/{found_by_record_id}/attachments")
    try:
        data = _upload_attachment_request(path, file_path)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(_strip_attachment_content(data), indent=2)


@mcp.tool(annotations=_READ_ONLY)
def get_testcases_by_query(
    filter_id: str = "",
    search: str = "",
    filters: JsonStrMap | None = None,
    fields: JsonStrList | None = None,
    page: int = 1,
    per_page: int = 300,
    formatted_text: bool = True,
    expand: JsonStrList | None = None,
) -> str:
    """
    Search test cases in the active project via a POST query body. Wraps
    POST /{projectID}/testCases/search. Handles both filtered searches and
    unfiltered listings; called with no args it returns the first page only (up
    to `per_page`, default 300). Paginate via `page`/`per_page` (see
    `paging.totalPages`/`totalCount` in the response) to retrieve them all. Sends
    parameters in a JSON body, avoiding URL-length and URL-encoding limits.

    Args:
        filter_id:      Saved-filter name or ID to apply.
        search:         Free-form Perforce ALM search expression. Field names are
                        labels that must match an existing field; matching is
                        case-insensitive (a nonexistent field 404s). String
                        literals use single or double quotes; there is no escape
                        for a quote inside a literal, so wrap a value containing '
                        in double quotes and a value containing " in single quotes.
                        Operators include =, !=, contains, and, or, plus parens.
                        Example: "Description contains 'WysiCorp'".
        filters:        Shorthand for equality-only filtering. Each key is a
                        field label — matched case-insensitively, but it must
                        name an existing field (multi-word labels like
                        "Multi Word Field" are supported — the shorthand
                        double-quotes labels automatically); each value is
                        matched as a string literal (quoted with whichever quote
                        preserves any literal quote in the value; a value with
                        both ' and " cannot be expressed and errors). Combined
                        with `and`.
                        If `search` is also supplied, the two are `and`-joined.
                        Example: {"Product": "Perforce ALM"}.
        fields:         Field labels to return on each item (case-insensitive,
                        e.g. "Summary", "Product"). Top-level props (id, number,
                        tag) are always returned and don't need listing.
        page:           Page number (default 1, min 1).
        per_page:       Items per page (default 300, max 1000).
        formatted_text: Return multi-line text as HTML (True) or plain text (False).
        expand:         Sub-objects to expand. Allowed values: attachments,
                        scripts, events, variants, steps, links, folders.

    Response: {
      "testCases": [<TestCase>, ...],  # each item: see get_testcase
      "paging": {"page": <int>, "pageLimit": <int>,
                 "totalPages": <int|null>, "totalCount": <int|null>}
    }
    Each item is shaped like `get_testcase`'s response; `fields` reflects what
    was requested via the `fields` param, and the expand-gated keys (scripts,
    attachments, events, variants, steps, links, folders) appear only when
    listed in `expand`. Chain test case `id` values into `get_testcase`,
    `update_testcases`, `list_testcase_steps`, `list_testcase_links`, or
    `add_automation_suite_testcases`.
    """
    body = _build_search_body(
        filter_id=filter_id, search=search, filters=filters, fields=fields,
        page=page, per_page=per_page, formatted_text=formatted_text, expand=expand,
    )

    path = _project_path("/testCases/search")

    try:
        data = _token_authenticated_request("POST", path, body=body)
    except Exception as e:
        return f"Error: {e}"
    for item in data.get("testCases", []):
        _strip_expanded_attachments(item)
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def get_testcase(
    item_id: int,
    fields: JsonStrList | None = None,
    formatted_text: bool = True,
    expand: JsonStrList | None = None,
) -> str:
    """
    Get a single test case by item ID. Wraps GET /{projectID}/testCases/{itemID}.

    Args:
        item_id:        Record ID of the test case.
        fields:         Field labels to return (case-insensitive, e.g. "Summary",
                        "Product"). Top-level props (id, number, tag) are always
                        returned and don't need listing.
        formatted_text: Return multi-line text as HTML (True) or plain text (False).
        expand:         Sub-objects to expand. Allowed values: attachments,
                        scripts, events, variants, steps, links, folders.

    Response: {
      "id": <int>, "number": <int>, "tag": <str>,
      "self": <str>, "ttstudioURL": <str>, "httpURL": <str>,
      "fields": [{"id": <int>, "label": <str>, "type": <str>, "<type>": <value>}, ...],
      # each field's value sits under a key named by its `type`, one of: string,
      # formattedString, menuItem, menuItemArray, editableVersion, boolean,
      # integer, decimal, date, dateTime, user, userArray
      # (e.g. {"id": 2, "label": "Summary", "type": "string", "string": "Login page"})
      # expand-gated, each present only when listed in `expand`:
      "scripts": <obj>, "attachments": <obj>, "events": <obj>,
      "variants": <obj>, "steps": <obj>, "links": <obj>, "folders": <obj>
    }
    `fields` reflects what was requested via the `fields` param. Chain `id`
    into `update_testcases`, `list_testcase_steps`, `list_testcase_links`,
    or `add_automation_suite_testcases`.
    """
    params: list[tuple[str, str]] = []
    if fields:
        params.extend(("fields", f) for f in fields)
    if not formatted_text:
        params.append(("formattedText", "false"))
    if expand:
        params.extend(("expand", e) for e in expand)

    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    path = _project_path(f"/testCases/{item_id}{query}")

    try:
        data = _token_authenticated_request("GET", path)
    except Exception as e:
        return f"Error: {e}"
    _strip_expanded_attachments(data)
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_WRITE_ADDITIVE)
def create_testcases(
    fields: list[dict],
    folders: JsonObjList | None = None,
    scripts: JsonObjList | None = None,
    variants: JsonObj | None = None,
    steps: JsonObj | None = None,
) -> str:
    """
    Create a test case in the active project. Wraps POST /{projectID}/testCases.

    To create multiple test cases, call this tool once per test case rather than
    batching — per-call responses make it easier to surface server-assigned IDs and
    isolate validation errors.

    Server-assigned values (id, number, tag, ttstudioURL, httpURL, events) are
    populated by Perforce ALM and should not be supplied.

    Args:
        fields:      List of field dicts. Identify each field by EITHER its integer
                     `id` OR its string `label` (the field's display name) — put the
                     name in `label`, never in `id`. Each dict also needs `type` (one
                     of: string, formattedString, menuItem, menuItemArray,
                     editableVersion, boolean, integer, decimal, date, dateTime,
                     user, userArray) and the
                     matching type-specific value key. Examples:
                       by id:    {"id": 5, "type": "string", "string": "Login form"}
                       by label: {"label": "Summary", "type": "string", "string": "Login form"}
                       menu:     {"label": "Product", "type": "menuItem", "menuItem": {"id": 132}}
        folders:     Folder placements as a list, e.g. [{"id": 135}] or
                     [{"path": "/Public/Product APS/v7.5 Release"}]. The tool
                     wraps this in {"foldersData": ...}.
        scripts:     Automated-test script references as a list, e.g.
                     [{"id": 95, "referenceType": "attachment"}]. The tool wraps
                     in {"scriptsData": ...}.
        variants:    Test variants as a dict with `included` and/or `excluded`
                     arrays, e.g. {"included": [{"id": 304, "label": "Client
                     Type", "type": "menuItemArray", "menuItemArray": [...]}],
                     "excluded": []}. The tool wraps in {"variantsData": ...}.
        steps:       Test steps as a TestCaseStepsData dict:
                       {"type": "detailed",          # "detailed" or "basic"
                        "detailed": [<entry>, ...]}   # supply ONLY the array matching
                                                      # `type` (use "basic" if basic) —
                                                      # never include both arrays
                     Prefer "detailed" unless the caller explicitly needs basic.
                     The tool wraps this in {"stepsData": ...}.

                     Each entry in the array is discriminated by its own `type`,
                     carrying the matching value key:
                       detailed entry types: "step" | "comment" | "shared"
                       basic entry types:    "step" | "comment" | "expectedResult"
                                             | "fileReferences" | "shared"
                       "step":    {"type": "step",
                                   "step": {"text": "...", "stepRows": [...]}}
                                  (omit step `number` — server-assigned/read-only)
                       "comment": {"type": "comment", "comment": "free text"}
                       "shared":  {"type": "shared", "shared": {"testCaseID": <int>}}
                       (basic-only "expectedResult"/"fileReferences" entries carry an
                        ExpectedResult / file-reference array directly; uncommon —
                        expected results normally live inside a step's stepRows.)

                     stepRows entries are likewise discriminated by `type`:
                       detailed rows: "expectedResult" | "stepNote"
                       basic rows:    "expectedResult" only
                       "expectedResult": {"type": "expectedResult",
                            "expectedResult": {"text": "...", "fileReferences": [
                                {"id": <int>, "referenceType": "attachment"}]}}
                            (fileReferences optional; referenceType is "attachment"
                             or "sourceFile")
                       "stepNote": {"type": "stepNote", "stepNote": "free text"}
                            (detailed only)

                     Complete detailed example:
                       {"type": "detailed", "detailed": [
                         {"type": "step", "step": {
                            "text": "Open the web client",
                            "stepRows": [
                              {"type": "expectedResult",
                               "expectedResult": {"text": "Login page appears",
                                                  "fileReferences": []}},
                              {"type": "stepNote",
                               "stepNote": "Use a supported browser"}]}}]}

    Note: if `folders` is supplied, folder placement is NOT atomic with the
    create. An invalid folder path/ID doesn't fail the whole request — the
    test case is still created, and every valid folder entry in the list is
    applied; an invalid entry is simply skipped and reported in `errors`, it
    does not block other entries (including ones later in the list) from
    being applied. When the REST API reports this as a 206 partial success
    (or any response with a non-empty `errors` array), this tool prepends a
    top-level "warning" key to the JSON response — re-fetch the created
    test case with expand=["folders"] rather than assuming the requested
    folders were applied.

    Response: {
      "testCases": [
        {"id": <int>, "number": <int>, "tag": <str>,
         "self": <str>, "ttstudioURL": <str>, "httpURL": <str>}
      ],
      "errors": [  # only present on 206 partial-success
        {"code": <str>, "message": <str>, "statusCode": <int>,
         "errorElementPath": <str>}, ...
      ],
      "warning": <str>  # only present on a 206/partial-success response that touched `folders` — see the Note above
    }
    The returned item carries identifier keys only — `fields` and sub-objects
    are NOT echoed back. Follow up with `get_testcase(id, expand=[...])` to
    read the populated record. Chain the new `id` into `update_testcases`,
    `list_testcase_steps`, `list_testcase_links`, or
    `add_automation_suite_testcases`. `errors` is only present on a 206
    partial-success response — most often from an invalid `folders` entry
    (see the Note above) — but if you see it, inspect `errorElementPath` to
    locate the cause.

    Links cannot be created here: the test case create endpoint silently
    discards any link payload (returns success but creates no link — verified
    empirically), so there is intentionally no `links` argument. After creating
    the test case, add links with create_testcase_links.

    Attachments cannot be set at creation either: this tool exposes no
    attachments argument, because the REST create endpoint silently discards
    any attachmentsData payload the same way — it returns success but attaches
    nothing. Add attachments after the test case exists with
    upload_testcase_attachment.
    """
    testcase: dict = {"fields": fields}
    if folders is not None:
        testcase["folders"] = {"foldersData": folders}
    if scripts is not None:
        testcase["scripts"] = {"scriptsData": scripts}
    if variants is not None:
        testcase["variants"] = {"variantsData": variants}
    if steps is not None:
        testcase["steps"] = {"stepsData": steps}

    body = {"testCases": [testcase]}
    path = _project_path("/testCases")

    try:
        data, status = _token_authenticated_request_with_status("POST", path, body)
    except Exception as e:
        return f"Error: {e}"
    warning = _folders_partial_success_warning(status, data, folders is not None, is_create=True)
    if warning:
        data = {"warning": warning, **data}
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_WRITE_DESTRUCTIVE)
def update_testcases(testcases: list[dict]) -> str:
    """
    Update one or more test cases in the active project. Wraps
    PUT /{projectID}/testCases. Pass a length-1 list to update a single item.

    Each dict must include `id` plus any fields/sub-objects to change.
    Sub-objects are supplied unwrapped — this tool wraps them into their
    envelopes ({"foldersData": [...]}, {"stepsData": {...}}, etc.) before
    sending.

    Note: if `folders` is supplied for an item, it REPLACES that test case's
    entire folder-placement collection wholesale (server-side behavior of the
    PUT endpoint) — passing an empty list removes the test case from every
    folder it is currently in, filing it nowhere. Omit `folders` from the
    payload to leave existing placements untouched.

    Warning: that folders replace is NOT atomic. If the folders list contains
    an invalid path or ID, the request can still change the test case's
    folder placements before reporting the error: the server removes the
    existing placements first, then applies every valid entry in the list —
    an invalid entry is simply skipped and reported in `errors`, it does not
    block other entries (including ones later in the list) from being
    applied — so a failed call can leave the test case filed in some, all,
    or none of the requested folders, with its original folders already
    gone regardless. When the REST API reports this as a 206 partial success
    (or any response with a non-empty `errors` array) for a
    request that touched `folders`, this tool prepends a top-level "warning"
    key to the JSON response — re-fetch the affected test case(s) with
    expand=["folders"] rather than assuming nothing changed.

    Note: if `steps` is supplied for an item, ALL existing step data on that
    test case is replaced wholesale (server-side behavior of the PUT endpoint).
    Omit `steps` from the payload to leave existing steps untouched.

    Links are not editable through this tool. Manage test case links with the
    dedicated link tools: create_testcase_links (add) and list_testcase_links
    (read).

    Attachments cannot be changed with a PUT and are rejected here: the REST
    update endpoint silently discards an attachments payload (returns success
    but changes nothing). upload_testcase_attachment is the only way to add
    one; there is no endpoint to edit or remove an existing attachment.

    Server-managed values (number, tag, ttstudioURL, httpURL, events) should
    not be supplied.

    Args:
        testcases: List of test case dicts. Per-item dict keys:
            id (required): Record ID of the test case to update.
            fields:        List of field dicts. Same shape as create_testcases:
                           identify each field by EITHER `id` (integer) OR `label`
                           (string field name, never placed in `id`), plus `type`
                           and the matching type-specific value key.
            folders:       Folder placements as a list, e.g. [{"id": 135}] or
                           [{"path": "/Public/Product APS/v7.5 Release"}]. Tool
                           wraps this in {"foldersData": ...}. Replaces ALL
                           existing folder placements.
            scripts:       Script references as a list; tool wraps in {"scriptsData": ...}.
            variants:      Variants dict ({"included": [...], "excluded": [...]});
                           tool wraps in {"variantsData": ...}. Replaces existing variants.
            steps:         Steps dict; supply only the array matching `type`, e.g.
                           {"type": "detailed", "detailed": [...]} (never both
                           arrays). See create_testcases `steps` for the full
                           structure. Tool wraps in {"stepsData": ...}. Replaces ALL
                           existing steps. Prefer "detailed" unless matching an
                           existing basic test case.

    Response: {
      "warning": <str>,  # only present on a 206/partial-success response that touched `folders` — see the Warning above
      "testCases": [
        {"id": <int>, "number": <int>, "tag": <str>,
         "self": <str>, "ttstudioURL": <str>, "httpURL": <str>},
        ...
      ],
      "errors": [  # only present on 206 partial-success
        {"code": <str>, "message": <str>, "statusCode": <int>,
         "errorElementPath": <str>}, ...
      ]
    }
    The `testCases` array has one entry per successful item. On 206
    partial-success, some items in the batch updated and others didn't —
    inspect each error's `errorElementPath` (e.g. "/testCases/2") to
    locate the failing input by index. As with `create_testcases`, the
    returned items carry identifier keys only — `fields` and sub-objects
    are NOT echoed back; follow up with `get_testcase(id, expand=[...])`
    to read the populated record.
    """
    allowed = {"id", "fields", "folders", "scripts", "variants", "steps"}
    not_updatable = {"links", "events", "attachments"}
    not_updatable_reasons = {
        "links": "links do not have an editing path. Add them with create_testcase_links instead of update_testcases.",
        "events": "events cannot be updated with PUT.",
        "attachments": "attachments cannot be updated with PUT. Add them with upload_testcase_attachment.",
    }
    wrapped: list[dict] = []
    for i, tc in enumerate(testcases):
        if "id" not in tc:
            return f"Error: testcases[{i}] missing the required 'id' key."
        blocked = not_updatable & set(tc)
        if blocked:
            reasons = "; ".join(not_updatable_reasons[k] for k in sorted(blocked))
            return f"Error: testcases[{i}] cannot include {sorted(blocked)} in an update: {reasons}"
        unknown = set(tc) - allowed - not_updatable
        if unknown:
            return f"Error: testcases[{i}] has unknown keys: {sorted(unknown)}"
        item: dict = {"id": tc["id"]}
        if "fields" in tc:
            item["fields"] = tc["fields"]
        if "folders" in tc:
            item["folders"] = {"foldersData": tc["folders"]}
        if "scripts" in tc:
            item["scripts"] = {"scriptsData": tc["scripts"]}
        if "variants" in tc:
            item["variants"] = {"variantsData": tc["variants"]}
        if "steps" in tc:
            item["steps"] = {"stepsData": tc["steps"]}
        wrapped.append(item)

    body = {"testCases": wrapped}
    path = _project_path("/testCases")

    try:
        data, status = _token_authenticated_request_with_status("PUT", path, body)
    except Exception as e:
        return f"Error: {e}"
    warning = _folders_partial_success_warning(status, data, any("folders" in item for item in wrapped))
    if warning:
        data = {"warning": warning, **data}
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def list_testcase_steps(item_id: int) -> str:
    """
    Get the steps for a test case. Wraps GET /{projectID}/testCases/{itemID}/steps.

    Args:
        item_id: Record ID of the test case.

    Response: {
      "self": <str>,        # REST href to /testCases/{id}/steps
      "stepsData": {
        "type": "basic" | "detailed",  # discriminator: which array below is populated
        "modifiedToCorrectSyntax": <bool>,  # optional; true if the server
                                            # auto-fixed step structure on read
        "basic": [<TestCaseBasicStepData>, ...],      # present when type == "basic"
        "detailed": [<TestCaseDetailedStepData>, ...] # present when type == "detailed"
      }
    }
    Each step entry carries its own `type` discriminator selecting a payload key:
    "step", "comment", "expectedResult", "fileReferences", "shared" for basic;
    "step", "comment", "shared" for detailed. Within a "step" entry, `stepRows`
    rows are themselves typed: "expectedResult" ({"text", "fileReferences"}) or,
    detailed-only, "stepNote" (string). See create_testcases `steps` for the full
    authoring shape. To learn the exact nested shape for each entry type, call
    this tool on an existing test case that uses the structure you need and
    inspect the returned payload.

    The project convention is "detailed" — prefer it for any new test cases or
    fresh step sets sent via update_testcase_steps. When round-tripping an
    existing test case, preserve whatever `type` came back (don't silently
    convert "basic" to "detailed", or vice versa).

    To do a targeted edit (add/remove/reorder a single step), pluck `stepsData`
    from this response, modify it locally, and pass the result back via
    update_testcase_steps — the PUT endpoint replaces all steps wholesale, so
    partial updates have to be done client-side.
    """
    path = _project_path(f"/testCases/{item_id}/steps")

    try:
        data = _token_authenticated_request("GET", path)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_WRITE_DESTRUCTIVE)
def update_testcase_steps(item_id: int, steps_data: dict) -> str:
    """
    Replace all steps on a test case. Wraps PUT /{projectID}/testCases/{itemID}/steps.

    WARNING: This endpoint is NOT a partial update. The provided `steps_data`
    replaces ALL existing steps on the test case. To add, remove, or modify
    individual steps, first call list_testcase_steps, modify the returned
    `stepsData` locally (insert/edit/delete entries in the `basic` and/or
    `detailed` arrays), then pass the full modified value back here.

    SECOND WARNING — silent reformatting: the server accepts a `type` switch
    (e.g. PUT type="detailed" against a test case currently using "basic")
    and will reformat the entire step set to match the new type. This is
    rarely intentional. ALWAYS read the current `stepsData.type` via
    list_testcase_steps before this call and supply the same value in
    `steps_data["type"]`, unless you specifically want to convert the test
    case's step structure.

    Args:
        item_id:    Record ID of the test case.
        steps_data: The TestCaseStepsData object — typically the `stepsData`
                    field of a list_testcase_steps response, with local edits
                    applied. Shape: {"type": "detailed", "detailed": [...]} —
                    supply ONLY the array matching `type` ("detailed" or "basic"),
                    never both; `modifiedToCorrectSyntax` is read-side and can be
                    omitted. See create_testcases `steps` for the full entry and
                    stepRows structure (step/comment/shared entries;
                    expectedResult/stepNote rows). The tool wraps this in
                    {"stepsData": ...} before sending.
                    Prefer "detailed" for new step sets; when updating an
                    existing test case, keep `type` matching what came back
                    from list_testcase_steps (don't switch basic↔detailed).

    Response: {
      "updated": true,
      "test_case_id": <int>     # echoed from input
    }
    Synthesized client-side — the REST endpoint itself returns 204 No Content.
    `updated: true` only confirms the PUT returned a non-error status;
    failures surface as `"Error: ..."` strings from the standard error path.
    To confirm the new state, call list_testcase_steps again.
    """
    body = {"stepsData": steps_data}
    path = _project_path(f"/testCases/{item_id}/steps")

    try:
        _token_authenticated_request("PUT", path, body=body)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps({
        "updated": True,
        "test_case_id": item_id,
    }, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def list_testcase_links(item_id: int) -> str:
    """
    Get the links attached to a test case. Wraps
    GET /{projectID}/testCases/{itemID}/links.

    Useful for discovering the exact shape of a project-configured link
    definition (e.g. "Requirements tested by") before writing a create call:
    add the link by hand in the UI on one test case, fetch it with this tool,
    and copy the `linkDefinition.id`, `type`, and relationship structure into
    your create_testcases/create_testcase_links payload.

    Args:
        item_id: Record ID of the test case.

    Response: {
      "self": <str>,                # REST href to /testCases/{id}/links
      "linksData": [
        {
          "id": <int>,              # link record ID
          "comment": <str>,
          "linkDefinition": {"id": <int>, "name": <str>},
          "type": "peers" | "parentChildren",   # discriminator below
          "peers": [<LinkedItem>, ...],          # present when type == "peers"
          "parentChildren": {                    # present when type == "parentChildren"
            "parent": <LinkedItem>,
            "children": [<LinkedItem>, ...]
          }
        }, ...
      ]
    }
    Each LinkedItem: {"itemID": <int>, "itemType": "issues"|"testCases"|
    "testRuns"|"requirements"|"documents"|"automatedTestResults",
    "isSuspect": <bool>, "link": <str>}. The `linkDefinition.id` and `type`
    fields are what you'll need when constructing payloads for
    create_testcase_links or test-case create/update.
    """
    path = _project_path(f"/testCases/{item_id}/links")

    try:
        data = _token_authenticated_request("GET", path)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_WRITE_ADDITIVE)
def create_testcase_links(item_id: int, links: list[dict]) -> str:
    """
    Add one or more links to a test case. Wraps
    POST /{projectID}/testCases/{itemID}/links.

    Use this when the link relationship is known after the test case already
    exists (e.g. linking a new test case back to a requirement via the
    "Requirements tested by" link definition). The test-case create endpoint
    ignores inline link payloads (see create_testcases), so links can't be
    attached at creation and must be added here.

    Args:
        item_id: Record ID of the test case.
        links:   List of Link objects. Each needs `linkDefinition` ({"id": ...}
                 or {"name": ...}), `type` ("peers" or "parentChildren"), and
                 the matching relationship key — `peers` (LinkedItem array) for
                 peer-type links, or `parentChildren` ({"parent": LinkedItem,
                 "children": [LinkedItem, ...]}) for hierarchical links. Each
                 LinkedItem is {"itemID": <int>, "itemType": "requirements" |
                 "testCases" | "issues" | "testRuns" | "documents" |
                 "automatedTestResults"}. Each link may also carry an optional
                 `comment` (string) annotating the link. The tool wraps the list
                 in {"linksData": ...}. For `peers`-type links, `peers` must
                 include a LinkedItem for this test case itself alongside the
                 other side(s) — omitting it silently no-ops: the call returns
                 success with an empty `linksData`, creating nothing.

    Response (201 success): {
      "linksData": [<Link>, ...]   # full Link shape — see list_testcase_links
    }
    Response (206 partial success): {
      "linksData": [<Link>, ...],
      "errors": [
        {"code": <str>, "message": <str>, "statusCode": <int>,
         "errorElementPath": <str>}, ...   # e.g. "/linksData/2"
      ]
    }
    Unlike `create_testcases`, the response echoes the full Link objects
    (including the server-assigned `id`), so no follow-up GET is needed to
    read back the created links.
    """
    body = {"linksData": links}
    path = _project_path(f"/testCases/{item_id}/links")

    try:
        data = _token_authenticated_request("POST", path, body=body)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def list_testcase_attachments(item_id: int) -> str:
    """
    Get metadata for the files attached to a test case. Wraps
    GET /{projectID}/testCases/{itemID}/attachments. Returns metadata only, not
    file contents.

    Args:
        item_id: Record ID of the test case.

    Response: {
      "self": <str>,   # REST href to /testCases/{id}/attachments
      "attachmentsData": [
        {"id": <int>, "filename": <str>, "size": <int>,
         "created": <str|null>, "modified": <str|null>,
         "encodedFileID": <str>}, ...
      ]
    }
    `encodedFileID` identifies the file for download_attachment; `id` is the
    attachment record ID.
    """
    path = _project_path(f"/testCases/{item_id}/attachments")
    try:
        data = _token_authenticated_request("GET", path)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(_strip_attachment_content(data), indent=2)


@mcp.tool(annotations=_WRITE_ADDITIVE)
def upload_testcase_attachment(item_id: int, file_path: str) -> str:
    """
    Attach a local file to a test case. Wraps
    POST /{projectID}/testCases/{itemID}/attachments (multipart/form-data).

    Args:
        item_id:   Record ID of the test case.
        file_path: Path to a file on the machine running THIS MCP server (not
                   the caller's machine), confined to the server's configured
                   upload directory (PERFORCE_ALM_MCP_UPLOAD_DIR; defaults to
                   a folder under the system temp directory) — a path that
                   resolves outside it is rejected. The file's bytes are
                   uploaded and its base name is preserved as the attachment
                   filename.

    Response: {
      "attachmentsData": [
        {"id": <int>, "filename": <str>, "encodedFileID": <str>}
      ]
    }
    The returned `id` is the new attachment record ID; `encodedFileID`
    identifies the file for download_attachment. A missing/unreadable
    file_path, or one outside the upload root, returns an "Error:" string.
    """
    path = _project_path(f"/testCases/{item_id}/attachments")
    try:
        data = _upload_attachment_request(path, file_path)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(_strip_attachment_content(data), indent=2)


@mcp.tool(annotations=_READ_ONLY)
def list_menus() -> str:
    """
    List all menus (pick-list value sets) defined in the active project. Wraps
    GET /{projectID}/configs/menus.

    A "menu" is the set of valid values backing a field whose type is
    `menuItem` or `menuItemArray` (e.g. Product, Importance, Issue Severity).
    Each entry has an `id` and a `name`.

    To discover the valid values for a specific field:
    1. Call this tool and find the menu whose `name` matches the field. Names
       usually align with field labels but not always (e.g. the "Product"
       field on issues is backed by the "Issue Product" menu). If the
       mapping is ambiguous, use list_menu_fields to confirm.
    2. Call list_menu_items with that menu's id to get the values.

    Response: {
      "self": <str>,                # REST href to /configs/menus
      "menusData": [
        {
          "id": <int>,              # menu record ID
          "name": <str>,            # e.g. "Versions Impacted"
          "self": <str>,            # REST href to /configs/menus/{id}
          "items":  {"self": <str>}, # href stub — no inline items;
                                     # call list_menu_items(id) for values
          "fields": {"self": <str>}  # href stub — no inline fields;
                                     # call list_menu_fields(id) for fields
        }, ...
      ]
    }
    `items` and `fields` are stub containers — they expose only a `self`
    URL pointing at the corresponding list endpoint, not inline payloads.
    """
    try:
        data = _token_authenticated_request("GET", _project_path("/configs/menus"))
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def list_menu_items(menu_id: int) -> str:
    """
    List the valid values (items) for a menu. Wraps
    GET /{projectID}/configs/menus/{menuID}/items.

    Each item's `id` and `label` are what you supply when setting a
    `menuItem`-typed field on create/update/trigger operations, e.g.
    {"label": "Product", "type": "menuItem", "menuItem": {"id": 2}}.

    Use list_menus first to find the menu_id.

    Args:
        menu_id: Record ID of the menu (obtain from list_menus).

    Response: {
      "self": <str>,                # REST href to /configs/menus/{menu_id}/items
      "itemsData": [
        {
          "id": <int>,              # menu item record ID
          "label": <str>,           # display label, e.g. "WysiCorp"
          "self": <str>,            # REST href to /configs/menus/{menu_id}/items/{id}
          "fieldStyle": {           # null when no style is configured
            "id": <int>,
            "name": <str>,          # e.g. "Red"
            "link": <str>           # REST href to /config/fieldStyles/{id}
          }
        }, ...
      ]
    }
    Only `id` and `label` are needed to set a menuItem field value;
    `fieldStyle` is UI metadata (color/style for display) and can be ignored
    when constructing payloads.
    """
    try:
        data = _token_authenticated_request(
            "GET", _project_path(f"/configs/menus/{menu_id}/items")
        )
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def list_menu_fields(menu_id: int) -> str:
    """
    List the fields that reference a given menu. Wraps
    GET /{projectID}/configs/menus/{menuID}/fields.

    Use this to disambiguate when a menu name doesn't obviously match a field
    label — e.g. to confirm which menu backs the "Product" field on
    requirements vs. issues.

    Args:
        menu_id: Record ID of the menu (obtain from list_menus).

    Response: {
      "self": <str>,                # REST href to /configs/menus/{menu_id}/fields
      "fieldsData": [
        {
          "id": <int>,              # field record ID
          "itemType": "issues" | "requirements" | "documents" |
                      "testCases" | "testRuns" | "testVariants",
          "longName": <str>         # field name, e.g. "Fix Resolution"
        }, ...
      ]
    }
    Note: this `itemType` enum is a different set than the one on link tools
    — this one includes "testVariants" but not "automatedTestResults"; the
    two are not interchangeable for cross-tool reasoning.
    """
    try:
        data = _token_authenticated_request(
            "GET", _project_path(f"/configs/menus/{menu_id}/fields")
        )
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def get_document(
    item_id: int,
    fields: JsonStrList | None = None,
    formatted_text: bool = True,
    expand: JsonStrList | None = None,
    snapshot: int = 0,
) -> str:
    """
    Get a single requirement document by item ID. Wraps
    GET /{projectID}/documents/{itemID}.

    Args:
        item_id:        Record ID of the document.
        fields:         Field labels to return (case-insensitive, e.g. "Summary",
                        "Product"). Top-level props (id, number, tag) are always
                        returned and don't need listing.
        formatted_text: Return multi-line text as HTML (True) or plain text (False).
        expand:         Sub-objects to expand. Allowed values: attachments,
                        events, snapshots, links, folders.
        snapshot:       Document snapshot number (0 = current).

    Response: {
      "id": <int>, "number": <int>, "tag": <str>,
      "self": <str>, "ttstudioURL": <str>, "httpURL": <str>,
      "fields": [{"id": <int>, "label": <str>, "type": <str>, "<type>": <value>}, ...],
      # each field's value sits under a key named by its `type`, one of: string,
      # formattedString, menuItem, menuItemArray, editableVersion, boolean,
      # integer, decimal, date, dateTime, user, userArray
      # (e.g. {"id": 2, "label": "Summary", "type": "string", "string": "Login page"})
      # expand-gated, each present only when listed in `expand`:
      "attachments": <obj>, "events": <obj>, "snapshots": <obj>,
      "links": <obj>, "folders": <obj>
    }
    `fields` reflects what was requested via the `fields` param. Chain `id`
    into `get_document_tree`, `list_document_snapshots`,
    `create_document_snapshot`, or back into get_document with a different
    `snapshot` to fetch historical versions.
    """
    params: list[tuple[str, str]] = []
    if fields:
        params.extend(("fields", f) for f in fields)
    if not formatted_text:
        params.append(("formattedText", "false"))
    if expand:
        params.extend(("expand", e) for e in expand)
    if snapshot != 0:
        params.append(("snapshot", str(snapshot)))

    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    path = _project_path(f"/documents/{item_id}{query}")

    try:
        data = _token_authenticated_request("GET", path)
    except Exception as e:
        return f"Error: {e}"
    _strip_expanded_attachments(data)
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def get_documents_by_query(
    filter_id: str = "",
    search: str = "",
    filters: JsonStrMap | None = None,
    fields: JsonStrList | None = None,
    page: int = 1,
    per_page: int = 300,
    formatted_text: bool = True,
    expand: JsonStrList | None = None,
) -> str:
    """
    Search requirement documents in the active project via a POST query body.
    Wraps POST /{projectID}/documents/search. Handles both filtered searches and
    unfiltered listings; called with no args it returns the first page only (up to
    `per_page`, default 300). Paginate via `page`/`per_page` (see
    `paging.totalPages`/`totalCount` in the response) to retrieve them all.

    Args:
        filter_id:      Saved-filter name or ID to apply.
        search:         Free-form Perforce ALM search expression. Field names are
                        labels that must match an existing field; matching is
                        case-insensitive (a nonexistent field 404s). String
                        literals use single or double quotes; there is no escape
                        for a quote inside a literal, so wrap a value containing '
                        in double quotes and a value containing " in single quotes.
                        Operators include =, !=, contains, and, or, plus parens.
                        Example: "Title contains 'Release Notes'".
        filters:        Shorthand for equality-only filtering. Each key is a
                        field label — matched case-insensitively, but it must
                        name an existing field (multi-word labels like
                        "Multi Word Field" are supported — the shorthand
                        double-quotes labels automatically); each value is
                        matched as a string literal (quoted with whichever quote
                        preserves any literal quote in the value; a value with
                        both ' and " cannot be expressed and errors). Combined
                        with `and`.
                        If `search` is also supplied, the two are `and`-joined.
                        Example: {"Status": "Approved"}.
        fields:         Field labels to return on each item (case-insensitive).
                        Top-level props (id, number, tag) are always returned
                        and don't need listing.
        page:           Page number (default 1, min 1).
        per_page:       Items per page (default 300, max 1000).
        formatted_text: Return multi-line text as HTML (True) or plain text (False).
        expand:         Sub-objects to expand. Allowed values: attachments,
                        events, snapshots, links, folders.

    Response: {
      "documents": [<Document>, ...],  # each item: see get_document
      "paging": {"page": <int>, "pageLimit": <int>,
                 "totalPages": <int|null>, "totalCount": <int|null>}
    }
    Chain document `id` values into `get_document`, `get_document_tree`,
    `list_document_snapshots`, or `create_document_snapshot`. Each item is
    shaped like `get_document`'s response; `fields` reflects what was
    requested via the `fields` param, and the expand-gated keys (attachments,
    events, snapshots, links, folders) appear only when listed in `expand`.
    """
    body = _build_search_body(
        filter_id=filter_id, search=search, filters=filters, fields=fields,
        page=page, per_page=per_page, formatted_text=formatted_text, expand=expand,
    )

    path = _project_path("/documents/search")

    try:
        data = _token_authenticated_request("POST", path, body=body)
    except Exception as e:
        return f"Error: {e}"
    for item in data.get("documents", []):
        _strip_expanded_attachments(item)
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_WRITE_ADDITIVE)
def create_documents(
    fields: list[dict],
    folders: JsonObjList | None = None,
) -> str:
    """
    Create a requirement document in the active project. Wraps
    POST /{projectID}/documents.

    To create multiple documents, call this tool once per document rather than
    batching — per-call responses make it easier to surface server-assigned IDs
    and isolate validation errors.

    Server-assigned values (id, number, tag, self, ttstudioURL, httpURL, events,
    snapshots) are populated by Perforce ALM and should not be supplied. Add
    snapshots after creation with create_document_snapshot.

    Args:
        fields:      List of field dicts. Identify each field by EITHER its integer
                     `id` OR its string `label` (the field's display name) — put the
                     name in `label`, never in `id`. Each dict also needs `type` (one
                     of: string, formattedString, menuItem, menuItemArray,
                     editableVersion, boolean, integer, decimal, date, dateTime,
                     user, userArray) and the
                     matching type-specific value key. Examples:
                       by id:    {"id": 2, "type": "string", "string": "Release Notes"}
                       by label: {"label": "Name", "type": "string", "string": "Release Notes"}
                       menu:     {"label": "Product", "type": "menuItem", "menuItem": {"id": 132}}
        folders:     Folder placements as a list, e.g. [{"id": 135}] or
                     [{"path": "/Public/Product APS/v7.5 Release"}]. The tool
                     wraps this in {"foldersData": ...}.

    Note: if `folders` is supplied, folder placement is NOT atomic with the
    create. An invalid folder path/ID doesn't fail the whole request — the
    document is still created, and every valid folder entry in the list is
    applied; an invalid entry is simply skipped and reported in `errors`, it
    does not block other entries (including ones later in the list) from
    being applied. When the REST API reports this as a 206 partial success
    (or any response with a non-empty `errors` array), this tool prepends a
    top-level "warning" key to the JSON response — re-fetch the created
    document with expand=["folders"] rather than assuming the requested
    folders were applied.

    Response: {
      "documents": [
        {"id": <int>, "number": <int>, "tag": <str>,
         "self": <str>, "ttstudioURL": <str>, "httpURL": <str>}
      ],
      "errors": [  # only present on 206 partial-success
        {"code": <str>, "message": <str>, "statusCode": <int>,
         "errorElementPath": <str>}, ...
      ],
      "warning": <str>  # only present on a 206/partial-success response that touched `folders` — see the Note above
    }
    The `documents` array always has length 1 (this tool creates one per call,
    even though the REST endpoint is bulk). Chain the returned `id` into
    get_document, get_document_tree, or list_document_snapshots. `errors` is
    only present on a 206 partial-success response — most often from an
    invalid `folders` entry (see the Note above) — but if you see it, inspect
    `errorElementPath` to locate the cause.

    Links cannot be created here: the documents create endpoint silently
    discards any link payload (returns success but creates no link — verified
    empirically), so there is intentionally no `links` argument. Add links
    after the document exists with create_document_links.

    Attachments cannot be set at creation either: this tool exposes no
    attachments argument, because the REST create endpoint silently discards
    any attachmentsData payload the same way — it returns success but attaches
    nothing. Add attachments after the document exists with
    upload_document_attachment.
    """
    document: dict = {"fields": fields}
    if folders is not None:
        document["folders"] = {"foldersData": folders}

    body = {"documents": [document]}
    path = _project_path("/documents")

    try:
        data, status = _token_authenticated_request_with_status("POST", path, body)
    except Exception as e:
        return f"Error: {e}"
    warning = _folders_partial_success_warning(status, data, folders is not None, is_create=True)
    if warning:
        data = {"warning": warning, **data}
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_WRITE_DESTRUCTIVE)
def update_documents(documents: list[dict]) -> str:
    """
    Update one or more requirement documents in the active project. Wraps
    PUT /{projectID}/documents. Pass a length-1 list to update a single item.

    Each dict must include `id` plus any fields/sub-objects to change.
    Sub-objects are supplied unwrapped — this tool wraps them into their
    envelopes ({"foldersData": [...]}) before sending.

    Note: if `folders` is supplied for an item, it REPLACES that document's
    entire folder-placement collection wholesale (server-side behavior of the
    PUT endpoint) — passing an empty list removes the document from every
    folder it is currently in, filing it nowhere. Omit `folders` from the
    payload to leave existing placements untouched.

    Warning: that folders replace is NOT atomic. If the folders list contains
    an invalid path or ID, the request can still change the document's folder
    placements before reporting the error: the server removes the existing
    placements first, then applies every valid entry in the list — an
    invalid entry is simply skipped and reported in `errors`, it does not
    block other entries (including ones later in the list) from being
    applied — so a failed call can leave the document filed in some, all, or
    none of the requested folders, with its original folders already gone
    regardless. When the REST API reports this as a 206 partial success (or
    any response with a non-empty `errors` array) for a request that touched
    `folders`, this tool prepends a top-level "warning" key to the JSON
    response — re-fetch the affected document(s) with expand=["folders"]
    rather than assuming nothing changed.

    Links are not editable through this tool. Manage document links with the
    dedicated link tools: create_document_links (add) and list_document_links
    (read).

    Attachments cannot be changed with a PUT and are rejected here: the REST
    update endpoint silently discards an attachments payload (returns success
    but changes nothing). upload_document_attachment is the only way to add
    one; there is no endpoint to edit or remove an existing attachment.

    Server-managed values (number, tag, ttstudioURL, httpURL, events, snapshots)
    should not be supplied; add snapshots with create_document_snapshot.

    Args:
        documents: List of document dicts. Per-item dict keys:
            id (required): Record ID of the document to update.
            fields:        List of field dicts. Same shape as create_documents:
                           identify each field by EITHER `id` (integer) OR `label`
                           (string field name, never placed in `id`), plus `type`
                           and the matching type-specific value key.
            folders:       Folder placements as a list, e.g. [{"id": 135}] or
                           [{"path": "/Public/Product APS/v7.5 Release"}]. Tool
                           wraps this in {"foldersData": ...}. Replaces ALL
                           existing folder placements.

    Response: {
      "warning": <str>,  # only present on a 206/partial-success response that touched `folders` — see the Warning above
      "documents": [
        {"id": <int>, "number": <int>, "tag": <str>,
         "self": <str>, "ttstudioURL": <str>, "httpURL": <str>},
        ...
      ],
      "errors": [  # only present on 206 partial-success
        {"code": <str>, "message": <str>, "statusCode": <int>,
         "errorElementPath": <str>}, ...
      ]
    }
    The `documents` array has one entry per successful item. On 206
    partial-success, some items updated and others didn't — inspect each error's
    `errorElementPath` (e.g. "/documents/2") to locate the failing input by index.
    """
    allowed = {"id", "fields", "folders"}
    not_updatable = {"links", "events", "snapshots", "attachments"}
    not_updatable_reasons = {
        "links": "links do not have an editing path. Add them with create_document_links instead of update_documents.",
        "events": "events cannot be updated with PUT.",
        "snapshots": "snapshots cannot be updated with PUT. Add them with create_document_snapshot.",
        "attachments": "attachments cannot be updated with PUT. Add them with upload_document_attachment.",
    }
    wrapped: list[dict] = []
    for i, doc in enumerate(documents):
        if "id" not in doc:
            return f"Error: documents[{i}] missing the required 'id' key."
        blocked = not_updatable & set(doc)
        if blocked:
            reasons = "; ".join(not_updatable_reasons[k] for k in sorted(blocked))
            return f"Error: documents[{i}] cannot include {sorted(blocked)} in an update: {reasons}"
        unknown = set(doc) - allowed - not_updatable
        if unknown:
            return f"Error: documents[{i}] has unknown keys: {sorted(unknown)}"
        item: dict = {"id": doc["id"]}
        if "fields" in doc:
            item["fields"] = doc["fields"]
        if "folders" in doc:
            item["folders"] = {"foldersData": doc["folders"]}
        wrapped.append(item)

    body = {"documents": wrapped}
    path = _project_path("/documents")

    try:
        data, status = _token_authenticated_request_with_status("PUT", path, body)
    except Exception as e:
        return f"Error: {e}"
    warning = _folders_partial_success_warning(status, data, any("folders" in item for item in wrapped))
    if warning:
        data = {"warning": warning, **data}
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def get_document_tree(
    item_id: int,
    expand: JsonStrList | None = None,
    recursive: bool = False,
    snapshot: int = 0,
) -> str:
    """
    Get a requirement document's tree. Wraps
    GET /{projectID}/documentTrees/{itemID}.

    To retrieve the *full* document tree (every node at every depth) in a
    single call, pass BOTH `expand=["nodes"]` AND `recursive=True`. Either
    flag alone is insufficient:
      - Without `expand=["nodes"]`, the response is just document metadata
        (id, name, documentLink, self) with no nodes.
      - With `expand=["nodes"]` but `recursive=False`, you get only the
        top-level nodes; each node's `childNodes` field will be absent.

    The returned nodes contain only tree-position data — `id`,
    `outlineNumber`, `tag`, `requirementID`, `requirementLink`. To read the
    actual requirement fields (Summary, Description, custom fields, etc.),
    follow up with `get_requirement` per `requirementID`.

    Args:
        item_id:   Record ID of the requirement document.
        expand:    Sub-objects to expand. Only allowed value: "nodes".
        recursive: When `expand=["nodes"]` is also set, embed every node's
                   childNodes recursively all the way down. Has no effect
                   on its own.
        snapshot:  Document snapshot ID (0 = current snapshot).

    Response: {
      "id": <int>,             # document record ID
      "name": <str>,           # document name
      "documentLink": <str>,   # REST href to the document
      "self": <str>,           # REST href to this tree
      "nodes": {               # only when "nodes" in expand
        "self": <str>,
        "nodesData": [
          {
            "id": <int>,             # node ID (NOT the requirement ID)
            "outlineNumber": <str>,  # e.g. "2.1"
            "tag": <str>,            # requirement tag, e.g. "FR-2"
            "requirementID": <int>,
            "requirementLink": <str>,
            "self": <str>,
            "childNodes": {          # only when recursive=true
              "self": <str>,
              "childNodes": [<Node>, ...]  # same Node shape, nested
            }
          }, ...
        ]
      }
    }
    Chain each node's `requirementID` into `get_requirement` to read the
    requirement's field values — nodes themselves carry only tree-position
    data. To batch the lookup, note that `get_requirements_by_query` cannot
    search by record ID (the search syntax has no `id` field); instead OR the
    node `tag`s, e.g. search="tag = 'FR-1' or tag = 'BR-7'". Caveat: Tag
    matching is prefix-based, so "tag = 'FR-1'" also matches FR-10, FR-100,
    etc. — over-returning is possible, so verify the returned tags against the
    set you asked for. Chain a node's `id` into `add_document_tree_nodes` as
    `parent_node_id` to attach new children under it.
    """
    params: list[tuple[str, str]] = []
    if expand:
        params.extend(("expand", e) for e in expand)
    if recursive:
        params.append(("recursive", "true"))
    if snapshot != 0:
        params.append(("snapshot", str(snapshot)))

    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    path = _project_path(f"/documentTrees/{item_id}{query}")

    try:
        data = _token_authenticated_request("GET", path)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_WRITE_ADDITIVE)
def add_document_tree_nodes(
    item_id: int,
    requirement_ids: list[int],
    parent_node_id: int = 0,
) -> str:
    """
    Add one or more existing requirements as nodes in a requirement document
    tree. Wraps both:
      POST /{projectID}/documentTrees/{itemID}/nodes              (top level)
      POST /{projectID}/documentTrees/{itemID}/nodes/{nodeID}/childNodes
                                                                   (under a node)

    Picks the endpoint based on `parent_node_id`:
      - 0 (default) → add at the top level of the document.
      - non-zero    → add as children of the node with that ID.

    The requirements must already exist in the project; this only attaches
    them to the tree. The server assigns each new node its `id`,
    `outlineNumber`, and `tag` — those come back in the response.

    Args:
        item_id:         Record ID of the requirement document.
        requirement_ids: Record IDs of existing requirements to attach.
        parent_node_id:  Tree node ID to attach under (0 = top level of the
                         document, the default).

    Response (parent_node_id == 0): {
      "nodesData": [<Node>, ...]
    }
    Response (parent_node_id != 0): {
      "childNodesData": [<Node>, ...]
    }
    Each Node has the same shape as in `get_document_tree`'s response:
      {"id": <int>, "outlineNumber": <str>, "tag": <str>,
       "requirementID": <int>, "requirementLink": <str>, "self": <str>}
    `id`, `outlineNumber`, `tag`, `requirementLink`, and `self` are all
    server-assigned; `requirementID` is echoed back from the request.
    Returned items have no `childNodes` (fresh nodes have no children).
    Chain a returned `id` into another `add_document_tree_nodes` call as
    `parent_node_id` to nest further children under it.
    """
    if parent_node_id == 0:
        path = _project_path(f"/documentTrees/{item_id}/nodes")
        body = {"nodesData": [{"requirementID": rid} for rid in requirement_ids]}
    else:
        path = _project_path(f"/documentTrees/{item_id}/nodes/{parent_node_id}/childNodes")
        body = {"childNodesData": [{"requirementID": rid} for rid in requirement_ids]}

    try:
        data = _token_authenticated_request("POST", path, body=body)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def list_document_snapshots(item_id: int) -> str:
    """
    List snapshots taken of a requirement document. Wraps
    GET /{projectID}/documents/{itemID}/snapshots.

    Args:
        item_id: Record ID of the requirement document.

    Response: {
      "self": <str>,            # REST href to /documents/{itemID}/snapshots
      "snapshotsData": [
        {
          "label": <str>,            # snapshot name
          "comment": <str>,          # free-form comment
          "createdBy": {             # User who took the snapshot
            "id": <int>, "username": <str>,
            "firstName": <str>, "lastName": <str>, "mi": <str>
          },
          "createdDate": <str>,      # ISO 8601 date-time
          "link": <str>,             # REST href to the snapshot
          "snapshot": <int>          # snapshot ID (>=1)
        }, ...
      ]
    }
    Chain a snapshot's `snapshot` value into `get_document_tree(snapshot=...)`
    or `get_document(snapshot=...)` to retrieve the document tree or document
    state as it existed at that point in time.
    """
    path = _project_path(f"/documents/{item_id}/snapshots")
    try:
        data = _token_authenticated_request("GET", path)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_WRITE_ADDITIVE)
def create_document_snapshot(
    item_id: int,
    label: str,
    comment: str = "",
) -> str:
    """
    Create a snapshot of a requirement document's current state. Wraps
    POST /{projectID}/documents/{itemID}/snapshots. The snapshot freezes
    the document tree as-is so it can later be retrieved via
    `get_document_tree(snapshot=<id>)`.

    The underlying REST endpoint accepts an array of snapshots in a single
    call, but the operation's summary is singular and snapshots are almost
    always created one at a time — this tool exposes only the single-snapshot
    form for that reason. Call again to create more.

    Args:
        item_id: Record ID of the requirement document.
        label:   Snapshot label (required, e.g. "First revision").
        comment: Optional free-text description (e.g. "Ready for review").

    Response: {
      "snapshotsData": [<Snapshot>]    # length 1; see list_document_snapshots
                                       # for full Snapshot shape (label, comment,
                                       # createdBy, createdDate, link, snapshot)
    }
    The `snapshotsData` array always has length 1 (this tool creates one
    snapshot per call, even though the REST endpoint is bulk). Note the
    top-level shape differs from `list_document_snapshots` — no `self`
    wrapper on create responses. Chain the returned `snapshot` ID into
    `get_document_tree(snapshot=...)` or `get_document(snapshot=...)` to
    read the frozen state.
    """
    snapshot: dict = {"label": label}
    if comment:
        snapshot["comment"] = comment
    body = {"snapshotsData": [snapshot]}
    path = _project_path(f"/documents/{item_id}/snapshots")

    try:
        data = _token_authenticated_request("POST", path, body=body)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def list_document_links(item_id: int) -> str:
    """
    Get the links attached to a document. Wraps
    GET /{projectID}/documents/{itemID}/links.

    Useful for discovering the exact shape of a project-configured link
    definition before writing a create call: add the link by hand in the UI on
    one document, fetch it with this tool, and copy the `linkDefinition.id`,
    `type`, and relationship structure into your create_document_links payload.

    Args:
        item_id: Record ID of the document.

    Response: {
      "self": <str>,                # REST href to /documents/{id}/links
      "linksData": [
        {
          "id": <int>,              # link record ID
          "comment": <str>,
          "linkDefinition": {"id": <int>, "name": <str>},
          "type": "peers" | "parentChildren",   # discriminator below
          "peers": [<LinkedItem>, ...],          # present when type == "peers"
          "parentChildren": {                    # present when type == "parentChildren"
            "parent": <LinkedItem>,
            "children": [<LinkedItem>, ...]
          }
        }, ...
      ]
    }
    Each LinkedItem: {"itemID": <int>, "itemType": "issues"|"testCases"|
    "testRuns"|"requirements"|"documents"|"automatedTestResults",
    "isSuspect": <bool>, "link": <str>}. The `linkDefinition.id` and `type`
    fields are what you'll need when constructing payloads for
    create_document_links.
    """
    path = _project_path(f"/documents/{item_id}/links")

    try:
        data = _token_authenticated_request("GET", path)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_WRITE_ADDITIVE)
def create_document_links(item_id: int, links: list[dict]) -> str:
    """
    Add one or more links to a document. Wraps
    POST /{projectID}/documents/{itemID}/links.

    This is the supported path for managing document links: the documents
    create endpoint ignores inline link payloads (see create_documents), so
    links can't be set at creation and must be added here, after the document
    exists (e.g. linking a document to a related requirement, test case, or
    issue).

    Args:
        item_id: Record ID of the document.
        links:   List of Link objects. Each needs `linkDefinition` ({"id": ...}
                 or {"name": ...}), `type` ("peers" or "parentChildren"), and
                 the matching relationship key — `peers` (LinkedItem array) for
                 peer-type links, or `parentChildren` ({"parent": LinkedItem,
                 "children": [LinkedItem, ...]}) for hierarchical links. Each
                 LinkedItem is {"itemID": <int>, "itemType": "requirements" |
                 "testCases" | "issues" | "testRuns" | "documents" |
                 "automatedTestResults"}. Each link may also carry an optional
                 `comment` (string) annotating the link. The tool wraps the list
                 in {"linksData": ...}. For `peers`-type links, `peers` must
                 include a LinkedItem for this document itself alongside the
                 other side(s) — omitting it silently no-ops: the call returns
                 success with an empty `linksData`, creating nothing.

    Response (201 success): {
      "linksData": [<Link>, ...]   # full Link shape — see list_document_links
    }
    Response (206 partial success): {
      "linksData": [<Link>, ...],
      "errors": [
        {"code": <str>, "message": <str>, "statusCode": <int>,
         "errorElementPath": <str>}, ...   # e.g. "/linksData/2"
      ]
    }
    The response echoes the full Link objects (including the server-assigned
    `id`), so no follow-up GET is needed to read back the created links.
    """
    body = {"linksData": links}
    path = _project_path(f"/documents/{item_id}/links")

    try:
        data = _token_authenticated_request("POST", path, body=body)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def list_document_attachments(item_id: int) -> str:
    """
    Get metadata for the files attached to a requirement document. Wraps
    GET /{projectID}/documents/{itemID}/attachments. Returns metadata only, not
    file contents.

    Args:
        item_id: Record ID of the document.

    Response: {
      "self": <str>,   # REST href to /documents/{id}/attachments
      "attachmentsData": [
        {"id": <int>, "filename": <str>, "size": <int>,
         "created": <str|null>, "modified": <str|null>,
         "encodedFileID": <str>}, ...
      ]
    }
    `encodedFileID` identifies the file for download_attachment; `id` is the
    attachment record ID.
    """
    path = _project_path(f"/documents/{item_id}/attachments")
    try:
        data = _token_authenticated_request("GET", path)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(_strip_attachment_content(data), indent=2)


@mcp.tool(annotations=_WRITE_ADDITIVE)
def upload_document_attachment(item_id: int, file_path: str) -> str:
    """
    Attach a local file to a requirement document. Wraps
    POST /{projectID}/documents/{itemID}/attachments (multipart/form-data).

    Args:
        item_id:   Record ID of the document.
        file_path: Path to a file on the machine running THIS MCP server (not
                   the caller's machine), confined to the server's configured
                   upload directory (PERFORCE_ALM_MCP_UPLOAD_DIR; defaults to
                   a folder under the system temp directory) — a path that
                   resolves outside it is rejected. The file's bytes are
                   uploaded and its base name is preserved as the attachment
                   filename.

    Response: {
      "attachmentsData": [
        {"id": <int>, "filename": <str>, "encodedFileID": <str>}
      ]
    }
    The returned `id` is the new attachment record ID; `encodedFileID`
    identifies the file for download_attachment. A missing/unreadable
    file_path, or one outside the upload root, returns an "Error:" string.
    """
    path = _project_path(f"/documents/{item_id}/attachments")
    try:
        data = _upload_attachment_request(path, file_path)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(_strip_attachment_content(data), indent=2)


@mcp.tool(annotations=_LOCAL_FILE_WRITE)
def download_attachment(encoded_file_id: str, destination_dir: str = "", overwrite: bool = False) -> str:
    """
    Download a file attachment to disk on the machine running THIS MCP server.
    Wraps GET /{projectID}/files/{encodedFileID} — the single endpoint every
    file download goes through, regardless of item type or where the
    attachment lives (a requirement/test case/document attachment, an event
    attachment, a found-by record attachment, etc.).

    Writes are confined to the server's configured download directory
    (PERFORCE_ALM_MCP_DOWNLOAD_DIR; defaults to a folder under the system temp
    directory). The on-disk filename comes from the download's
    Content-Disposition header, reduced to a bare filename, falling back to
    encoded_file_id when the header is absent — the caller only chooses the
    (optional) sub-directory within the download root. An existing file at the
    resolved destination is left untouched unless overwrite is True.

    Args:
        encoded_file_id:  The `encodedFileID` value from an attachment record —
                          e.g. an entry in list_*_attachments' attachmentsData,
                          or the attachmentsData returned by upload_*_attachment.
        destination_dir:  Optional sub-directory under the server's download
                          root to write into. Must resolve inside the root; a
                          path that escapes it (an absolute path outside the
                          root, "..", etc.) is rejected. Empty writes to the
                          root itself.
        overwrite:        If False (default), an existing file at the resolved
                          destination is left untouched and an "Error:" string
                          is returned instead of writing. Set True to replace
                          it. The attachment is still downloaded from the
                          server either way before this check runs, since the
                          filename isn't known until the download responds.

    Response: {"destination_path": <str>, "filename": <str>, "bytes_written": <int>}
    A destination that escapes the download root, an existing file with
    overwrite not set, an invalid/unknown encoded_file_id, or an unwritable
    destination returns an "Error:" string.
    """
    path = _project_path(f"/files/{urllib.parse.quote(encoded_file_id, safe='')}")
    try:
        target_dir = _resolve_under_root(_download_root(), destination_dir)
        os.makedirs(target_dir, exist_ok=True)
        file_bytes, server_name = _download_file_request(path)
        filename = _safe_leaf_name(server_name, _safe_leaf_name(encoded_file_id, "attachment"))
        destination_path = os.path.join(target_dir, filename)
        try:
            with open(destination_path, "wb" if overwrite else "xb") as fh:
                fh.write(file_bytes)
        except FileExistsError:
            return f"Error: The destination path already exists: {destination_path!r}. Set overwrite=True to replace it."
    except Exception as e:
        return f"Error: {e}"
    return json.dumps({
        "destination_path": destination_path,
        "filename": filename,
        "bytes_written": len(file_bytes),
    }, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def list_automation_suites(
    test_case_id: int = 0,
    automation_script_id: str = "",
    expand: JsonStrList | None = None,
) -> str:
    """
    List automation suites in the active project. Wraps
    GET /{projectID}/automationSuites.

    Unlike most list endpoints in this server, this one is not a search — it
    only supports the two equality filters below. There is no POST /search
    counterpart for automation suites.

    Args:
        test_case_id:         Filter to suites that include this test case
                              record ID. 0 disables the filter.
        automation_script_id: Filter to suites that include the given
                              automated test script ID. Empty string disables.
        expand:               Sub-objects to expand. Only allowed value: "testCases".

    Response: {
      "self": <str>,                   # REST href to /automationSuites
      "automationSuitesData": [
        <AutomationSuite>, ...         # see get_automation_suite for full shape
                                       # (id, name, self, description, active,
                                       # scriptIDPrefix, createdInfo, modifiedInfo,
                                       # owners, runConfiguration, and expand-gated
                                       # testCases)
      ]
    }
    Chain each suite's `id` into `get_automation_suite`, `update_automation_suite`,
    `run_automation_suite`, `list_automation_suite_testcases`,
    `add_automation_suite_testcases`, `remove_automation_suite_testcase`, or
    `list_automation_suite_builds`.
    """
    params: list[tuple[str, str]] = []
    if test_case_id:
        params.append(("testCaseID", str(test_case_id)))
    if automation_script_id:
        params.append(("automationScriptID", automation_script_id))
    if expand:
        params.extend(("expand", e) for e in expand)

    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    path = _project_path(f"/automationSuites{query}")

    try:
        data = _token_authenticated_request("GET", path)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def get_automation_suite(
    automation_suite_id: int,
    expand: JsonStrList | None = None,
) -> str:
    """
    Get a single automation suite by ID. Wraps
    GET /{projectID}/automationSuites/{automationSuiteID}.

    Args:
        automation_suite_id: Record ID of the automation suite.
        expand:              Sub-objects to expand. Only allowed value: "testCases".

    Response: {
      "id": <int>,                   # automation suite ID
      "name": <str>,
      "self": <str>,                 # REST href to /automationSuites/{id}
      "description": <str>,
      "active": <bool>,
      "scriptIDPrefix": <str>,       # prefix for unique test case tags
      "createdInfo": {               # TimestampInfo
        "user": <User>,              # {id, username, firstName, lastName, mi}
        "dateTime": <str>            # ISO 8601
      },
      "modifiedInfo": <TimestampInfo>,
      "owners": [<User>, ...],
      "runConfiguration": {          # discriminated by "type"
        "type": "jenkins",           # currently "jenkins" is the only value
        "automatedTestConfig": {...},
        ...                          # type-specific fields (e.g. Jenkins build params)
      },
      "testCases": {                 # only when "testCases" in expand
        "self": <str>,
        "testCasesData": [...]       # see list_automation_suite_testcases
      }
    }
    Unlike `list_automation_suites`, the response is the AutomationSuite
    object directly — there is no top-level `automationSuitesData` wrapper.
    Chain `id` into `update_automation_suite`, `run_automation_suite`,
    `list_automation_suite_testcases`, `add_automation_suite_testcases`,
    `remove_automation_suite_testcase`, or `list_automation_suite_builds`.
    For the full `runConfiguration` shape per discriminator type, see
    `create_automation_suites` (currently only `"type": "jenkins"` is supported).
    """
    params: list[tuple[str, str]] = []
    if expand:
        params.extend(("expand", e) for e in expand)

    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    path = _project_path(f"/automationSuites/{automation_suite_id}{query}")

    try:
        data = _token_authenticated_request("GET", path)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def list_automation_suite_testcases(automation_suite_id: int) -> str:
    """
    List the test cases associated with an automation suite. Wraps
    GET /{projectID}/automationSuites/{automationSuiteID}/testCases.

    For automation suites, `isDeleted` on each item is always false; the
    field exists on this shape because the same schema is reused by
    automation *build* test-case sets, where it can be true (a build
    references the full set of test cases as they existed at submit time
    even if some were later deleted from the project).

    Functionally equivalent to calling
    get_automation_suite(automation_suite_id, expand=["testCases"]) and
    reading the `testCases.testCasesData` field — prefer this tool when
    you only need the test-case set and want to skip the suite metadata.

    Args:
        automation_suite_id: Record ID of the automation suite.

    Response: {
      "self": <str>,             # REST href to /automationSuites/{id}/testCases
      "testCasesData": [
        {
          "id": <int>,             # test case ID
          "tag": <str>,            # e.g. "TC-143"
          "number": <int>,         # e.g. 143
          "isDeleted": <bool>,     # always false from this endpoint
          "link": <str>,           # REST href to /testCases/{id}
          "resultUniqueNames": [<str>, ...]  # unique-name strings that map
                                              # automated test results back
                                              # to this test case
        }, ...
      ]
    }
    Chain each item's `id` into `get_testcase`, `update_testcases`,
    `list_testcase_steps`, or `remove_automation_suite_testcase`.
    """
    path = _project_path(f"/automationSuites/{automation_suite_id}/testCases")

    try:
        data = _token_authenticated_request("GET", path)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_WRITE_ASSOCIATIVE)
def add_automation_suite_testcases(
    automation_suite_id: int,
    testcases: list[dict],
) -> str:
    """
    Add one or more existing test cases to an automation suite. Wraps
    POST /{projectID}/automationSuites/{automationSuiteID}/testCases.

    The test cases must already exist in the project; this only attaches
    them as members of the suite.

    Note: there is no PUT/PATCH on suite-test-case associations. To change
    `resultUniqueNames` for a test case already in the suite, you must
    remove it (remove_automation_suite_testcase) and re-add it with the
    new value.

    Args:
        automation_suite_id: Record ID of the automation suite.
        testcases:           List of test case dicts. Per-item dict keys:
            id (required):     Record ID of an existing test case in the
                               project.
            resultUniqueNames: Optional list of unique-name strings that
                               map automated test results back to this test
                               case. Results associate with a suite test
                               case in two ways: (1) the result's ScriptID
                               tag matches the test case, or (2) the
                               result's uniqueName matches an entry in
                               resultUniqueNames here.
            number:            Optional test case number. Usually omitted
                               (the test case is identified by `id`); if
                               supplied it is sent verbatim.
                           The tool wraps the list in {"testCasesData": ...}.

    Response: {
      "self": <str>,             # REST href to /automationSuites/{id}/testCases
      "testCasesData": [
        {<AutomationTestCaseSetItem>}, ...
      ]
    }
    The response contains the *full updated* test-case set for the suite,
    not just the items added by this call. Each item has the same shape as
    `list_automation_suite_testcases`'s response:
      {id, tag, number, isDeleted, link, resultUniqueNames}
    Chain each item's `id` into `get_testcase`, `update_testcases`,
    `list_testcase_steps`, or `remove_automation_suite_testcase`.
    """
    body = {"testCasesData": testcases}
    path = _project_path(f"/automationSuites/{automation_suite_id}/testCases")

    try:
        data = _token_authenticated_request("POST", path, body=body)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_WRITE_DESTRUCTIVE)
def remove_automation_suite_testcase(
    automation_suite_id: int,
    test_case_id: int,
) -> str:
    """
    Remove a single test case from an automation suite. Wraps
    DELETE /{projectID}/automationSuites/{automationSuiteID}/testCases/{testCaseID}.

    Removes only the suite-membership association — the underlying test
    case itself is untouched and remains in the project. The REST endpoint
    returns 204 No Content on success; this tool synthesizes a small
    confirmation payload for visibility.

    Args:
        automation_suite_id: Record ID of the automation suite.
        test_case_id:        Record ID of the test case to remove from the
                             suite.

    Response: {
      "deleted": true,
      "automation_suite_id": <int>,   # echoed from input
      "test_case_id": <int>           # echoed from input
    }
    Synthesized client-side — the REST endpoint itself returns 204 No Content.
    `deleted: true` only confirms the DELETE returned a non-error status;
    failures surface as `"Error: ..."` strings from the standard error path.
    To verify the test case is no longer in the suite, call
    list_automation_suite_testcases.
    """
    path = _project_path(f"/automationSuites/{automation_suite_id}/testCases/{test_case_id}")

    try:
        _token_authenticated_request("DELETE", path)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps({
        "deleted": True,
        "automation_suite_id": automation_suite_id,
        "test_case_id": test_case_id,
    }, indent=2)


@mcp.tool(annotations=_WRITE_ADDITIVE)
def create_automation_suites(
    name: str,
    description: str = "",
    active: bool = True,
    script_id_prefix: str = "",
    owners: JsonObjList | None = None,
    run_configuration: JsonObj | None = None,
    testcases: JsonObjList | None = None,
) -> str:
    """
    Create an automation suite in the active project. Wraps
    POST /{projectID}/automationSuites.

    To create multiple suites, call this tool once per suite rather than
    batching — per-call responses make it easier to surface server-assigned
    IDs and isolate validation errors.

    Server-assigned values (id, self, createdInfo, modifiedInfo) are
    populated by Perforce ALM and should not be supplied.

    Args:
        name:              Suite name (required).
        description:       Optional free-text description.
        active:            Whether the suite is active (default True).
        script_id_prefix:  Optional prefix for unique test case tags.
        owners:            Optional list of User dicts identifying suite owners.
                           Identify each user by `username` (e.g.
                           [{"username": "smithj"}]) or by first + last name
                           (e.g. [{"firstName": "Jane", "lastName": "Smith"}]).
                           The record `id` also works, but there's no user-lookup
                           tool to obtain it — prefer username or name.
        run_configuration: Optional run configuration. Discriminated on
                           `type`; for "jenkins":
                           {
                             "type": "jenkins",
                             "automatedTestConfig": {"id": <int>},
                             "jenkins": {
                               "projectName": "...",
                               "remoteAuthenticationToken": "..." (optional),
                               "defaultBuildParameters": [
                                 {"name": "...", "type": "text",     "text":     "..."},
                                 {"name": "...", "type": "password", "password": "..."},
                                 {"name": "...", "type": "ignore"}
                               ]
                             }
                           }
                           Per the spec, on GET `remoteAuthenticationToken`
                           and password values are always returned as null.
        testcases:         Optional list of test case dicts to attach at
                           creation. Same per-item shape as
                           add_automation_suite_testcases. The tool wraps
                           in {"testCasesData": ...}.

    Response: {
      "self": <str>,                    # REST href to /automationSuites
      "automationSuitesData": [
        {<AutomationSuite>}              # see get_automation_suite for full shape
      ]
    }
    The `automationSuitesData` array always has length 1 (this tool creates
    one suite per call, even though the REST endpoint is bulk). Each item
    is shaped like `get_automation_suite`'s response — server-assigned
    fields (`id`, `self`, `createdInfo`, `modifiedInfo`) are populated.
    Chain the returned `id` into `get_automation_suite`,
    `update_automation_suite`, `run_automation_suite`,
    `list_automation_suite_testcases`, `add_automation_suite_testcases`,
    `remove_automation_suite_testcase`, or `list_automation_suite_builds`.
    """
    suite: dict = {"name": name, "active": active}
    if description:
        suite["description"] = description
    if script_id_prefix:
        suite["scriptIDPrefix"] = script_id_prefix
    if owners is not None:
        suite["owners"] = owners
    if run_configuration is not None:
        suite["runConfiguration"] = run_configuration
    if testcases is not None:
        suite["testCases"] = {"testCasesData": testcases}

    body = {"automationSuitesData": [suite]}
    path = _project_path("/automationSuites")

    try:
        data = _token_authenticated_request("POST", path, body=body)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_WRITE_DESTRUCTIVE)
def update_automation_suite(
    automation_suite_id: int,
    name: str | None = None,
    description: str | None = None,
    active: bool | None = None,
    script_id_prefix: str | None = None,
    owners: JsonObjList | None = None,
    run_configuration: JsonObj | None = None,
    testcases: JsonObjList | None = None,
) -> str:
    """
    Update an existing automation suite. Wraps
    PUT /{projectID}/automationSuites/{automationSuiteID}.

    Top-level scalar fields are partial-update — only the ones supplied
    are modified. Sub-objects (`testCases`, `runConfiguration`, `owners`)
    are REPLACED WHOLESALE when supplied; omit them to leave existing
    values untouched. For incremental test-case membership edits, prefer
    add_automation_suite_testcases / remove_automation_suite_testcase.

    Pass `""` (empty string) to clear a string field, or `None` (the
    default) to leave it untouched.

    Server-managed values (id, self, createdInfo, modifiedInfo) should
    not be supplied.

    Args:
        automation_suite_id: Record ID of the suite to update.
        name:                New suite name (None = skip).
        description:         New description (None = skip, "" = clear).
        active:              New active flag (None = skip).
        script_id_prefix:    New script ID prefix (None = skip, "" = clear).
        owners:              Replacement list of User dicts (None = skip,
                             [] = remove all owners). See create_automation_suites
                             for the User shape (username or first + last name).
        run_configuration:   Replacement run configuration (None = skip).
                             See create_automation_suites for the Jenkins
                             shape.
        testcases:           Replacement list of test case dicts (None =
                             skip). See add_automation_suite_testcases
                             for per-item shape. Tool wraps in
                             {"testCasesData": ...}.

    Response: {
      "updated": true,
      "automation_suite_id": <int>     # echoed from input
    }
    Synthesized client-side — the REST endpoint itself returns 204 No Content.
    `updated: true` only confirms the PUT returned a non-error status;
    failures surface as `"Error: ..."` strings from the standard error path.
    To verify the changes landed, call `get_automation_suite` (optionally
    with `expand=["testCases"]`).
    """
    suite: dict = {}
    if name is not None:
        suite["name"] = name
    if description is not None:
        suite["description"] = description
    if active is not None:
        suite["active"] = active
    if script_id_prefix is not None:
        suite["scriptIDPrefix"] = script_id_prefix
    if owners is not None:
        suite["owners"] = owners
    if run_configuration is not None:
        suite["runConfiguration"] = run_configuration
    if testcases is not None:
        suite["testCases"] = {"testCasesData": testcases}

    path = _project_path(f"/automationSuites/{automation_suite_id}")

    try:
        _token_authenticated_request("PUT", path, body=suite)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps({
        "updated": True,
        "automation_suite_id": automation_suite_id,
    }, indent=2)


@mcp.tool(annotations=_TRIGGER)
def run_automation_suite(
    automation_suite_id: int,
    test_run_set: JsonObj | None = None,
    build_parameters: JsonObjList | None = None,
    config_type: str = "jenkins",
) -> str:
    """
    Start a run of an automation suite that has a run configuration set.
    Wraps POST /{projectID}/automationSuites/{automationSuiteID}/run.

    The suite must already have a run configuration (see
    create_automation_suites / update_automation_suite).

    Build parameters supplied here OVERRIDE the defaults configured on
    the suite's run configuration. To suppress a configured default
    parameter, include it with type "ignore".

    Args:
        automation_suite_id: Record ID of the suite to run.
        test_run_set:        Optional MenuItem ({"id": <int>} or
                             {"label": "..."}) — the test run set to
                             associate the resulting build with.
        build_parameters:    Optional list of JenkinsBuildParameter dicts
                             when config_type is "jenkins". Each item is
                             one of:
                               {"name": "...", "type": "text",     "text":     "..."}
                               {"name": "...", "type": "password", "password": "..."}
                               {"name": "...", "type": "ignore"}
        config_type:         Run configuration type. Currently only
                             "jenkins" is supported by the REST API;
                             exposed for forward compatibility with future
                             configuration types.

    Response: {
      "id": <int>,                 # automation build ID
      "number": <str>,             # build number, e.g. "167"
      "submittedDate": <str|null>, # ISO 8601; null until results are submitted
      "self": <str>                # REST href to /automationSuites/{sid}/builds/{id}
    }
    Chain the returned `id` as `build_id` into `list_automation_build_results`,
    `get_automation_build_result`, or `associate_automation_results` once
    results have been reported back from the run. `submittedDate` is null
    right after this call and only becomes non-null after the executor
    (e.g. Jenkins) submits results to Perforce ALM.
    """
    body: dict = {"type": config_type}
    if test_run_set is not None:
        body["testRunSet"] = test_run_set
    if config_type == "jenkins" and build_parameters is not None:
        body["jenkins"] = {"buildParameters": build_parameters}

    path = _project_path(f"/automationSuites/{automation_suite_id}/run")

    try:
        data = _token_authenticated_request("POST", path, body=body)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def list_automation_suite_builds(
    automation_suite_id: int,
    number: str = "",
    number_match_type: str = "",
    branch: str = "",
    branch_match_type: str = "",
    description: str = "",
    description_match_type: str = "",
    source: str = "",
    source_match_type: str = "",
    date_min: str = "",
    date_max: str = "",
    start_date_min: str = "",
    start_date_max: str = "",
    duration_min: int | None = None,
    duration_max: int | None = None,
    results_blocked_min: int | None = None,
    results_blocked_max: int | None = None,
    results_passed_min: int | None = None,
    results_passed_max: int | None = None,
    results_failed_min: int | None = None,
    results_failed_max: int | None = None,
    results_skipped_min: int | None = None,
    results_skipped_max: int | None = None,
    results_unknown_min: int | None = None,
    results_unknown_max: int | None = None,
    results_total_min: int | None = None,
    results_total_max: int | None = None,
    status: JsonStrList | None = None,
    test_run_sets: JsonMixedList | None = None,
    users: JsonMixedList | None = None,
    protected: bool | None = None,
    sort_by: str = "",
    sort_order: str = "",
    include_total: bool | None = None,
    page: int = 1,
    per_page: int = 300,
) -> str:
    """
    List the automation builds for an automation suite, with filtering,
    sorting, and paging. Wraps
    GET /{projectID}/automationSuites/{automationSuiteID}/builds.

    Args:
        automation_suite_id: Record ID of the automation suite (required).

        Text filters — each value paired with a *_match_type ("exact" or
        "wildcard"; omit match_type to let the server pick its default):
            number, number_match_type
            branch, branch_match_type
            description, description_match_type
            source, source_match_type

        Date ranges (ISO 8601 strings, e.g. "2025-01-01T00:00:00Z";
        ranges are inclusive):
            date_min, date_max:             Submission/build date
            start_date_min, start_date_max: When the build started

        Numeric ranges (inclusive):
            duration_min, duration_max:           Build duration (ms)
            results_blocked_min, results_blocked_max
            results_passed_min, results_passed_max
            results_failed_min, results_failed_max
            results_skipped_min, results_skipped_max
            results_unknown_min, results_unknown_max
            results_total_min, results_total_max

        Multi-select filters (match-any semantics, comma-joined on the
        wire):
            status:        AutomationBuildStatus values — "started",
                           "building", "waitingOnResults", "finished",
                           "unknown".
            test_run_sets: Test run set IDs (int) or names (str), mixed
                           list allowed.
            users:         User record IDs (int) or login usernames (str),
                           mixed list allowed.

        Other:
            protected: Filter to builds protected from auto-deletion.
            sort_by:   AutomationBuildSortColumn enum — "branch", "number",
                       "status", "date", "description", "duration",
                       "results_blocked", "results_failed",
                       "results_passed", "results_skipped",
                       "results_unknown", "results_total", "source",
                       "start_Date" (note the capital D — that's verbatim
                       from the spec), "test_run_sets", "users",
                       "protected".
            sort_order: "asc" or "desc".

        Paging:
            page:          Page number (default 1).
            per_page:      Items per page (default 300, max 1000).
            include_total: Include total count in paging metadata; True
                           can slow the request server-side.

    Response: {
      "self": <str>,                   # REST href to /automationSuites/{id}/builds
      "buildsData": [
        {
          "id": <int>,                 # automation build ID
          "number": <str>,             # build number, e.g. "167"
          "submittedDate": <str|null>, # null until results submitted
          "self": <str>,               # REST href to /builds/{id}
          "description": <str>,
          "branch": <str>,
          "startDate": <str>,          # ISO 8601
          "duration": <int>,           # milliseconds
          "testRunSet": <MenuItem>,    # {id, label}
          "externalURL": <str>,        # link in the build tool
          "properties": [<AutomationBuildProperty>, ...],
          "status": <str>,             # AutomationBuildStatus: started, building,
                                       # waitingOnResults, finished, unknown
          "source": <str>,             # e.g. "Jenkins Plugin"
          "protected": <bool>,         # protected from auto-deletion
          "createdInfo": <TimestampInfo>,
          "modifiedInfo": <TimestampInfo>,
          "submittedInfo": <TimestampInfo>,
          "processingLog": <str>,      # build-processing errors, if any
          "resultsSummary": {          # counts per status
            "total": <int>, "passed": <int>, "failed": <int>,
            "skipped": <int>, "blocked": <int>, "unknown": <int>
          }
          # `results` and `testCases` are NOT populated by this endpoint —
          # fetch via list_automation_build_results /
          # list_automation_suite_testcases instead
        }, ...
      ],
      "paging": {"page": <int>, "pageLimit": <int>,
                 "totalPages": <int|null>, "totalCount": <int|null>}
    }
    Chain each build's `id` as `build_id` into `list_automation_build_results`,
    `get_automation_build_result`, or `associate_automation_results`.
    `totalCount` is null in `paging` unless `include_total=True` was passed.
    """
    params: list[tuple[str, str]] = []

    if number:
        params.append(("number", number))
    if number_match_type:
        params.append(("number_match_type", number_match_type))
    if branch:
        params.append(("branch", branch))
    if branch_match_type:
        params.append(("branch_match_type", branch_match_type))
    if description:
        params.append(("description", description))
    if description_match_type:
        params.append(("description_match_type", description_match_type))
    if source:
        params.append(("source", source))
    if source_match_type:
        params.append(("source_match_type", source_match_type))

    if date_min:
        params.append(("date[gte]", date_min))
    if date_max:
        params.append(("date[lte]", date_max))
    if start_date_min:
        params.append(("start_date[gte]", start_date_min))
    if start_date_max:
        params.append(("start_date[lte]", start_date_max))

    range_pairs: list[tuple[str, int | None]] = [
        ("duration[gte]",        duration_min),
        ("duration[lte]",        duration_max),
        ("results_blocked[gte]", results_blocked_min),
        ("results_blocked[lte]", results_blocked_max),
        ("results_passed[gte]",  results_passed_min),
        ("results_passed[lte]",  results_passed_max),
        ("results_failed[gte]",  results_failed_min),
        ("results_failed[lte]",  results_failed_max),
        ("results_skipped[gte]", results_skipped_min),
        ("results_skipped[lte]", results_skipped_max),
        ("results_unknown[gte]", results_unknown_min),
        ("results_unknown[lte]", results_unknown_max),
        ("results_total[gte]",   results_total_min),
        ("results_total[lte]",   results_total_max),
    ]
    for key, val in range_pairs:
        if val is not None:
            params.append((key, str(val)))

    if status:
        params.append(("status", ",".join(status)))
    if test_run_sets:
        params.append(("test_run_sets", ",".join(str(x) for x in test_run_sets)))
    if users:
        params.append(("users", ",".join(str(x) for x in users)))

    if protected is not None:
        params.append(("protected", "true" if protected else "false"))
    if sort_by:
        params.append(("sort_by", sort_by))
    if sort_order:
        params.append(("sort_order", sort_order))
    if include_total is not None:
        params.append(("include_total", "true" if include_total else "false"))
    if page != 1:
        params.append(("page", str(page)))
    if per_page != 300:
        params.append(("per_page", str(per_page)))

    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    path = _project_path(f"/automationSuites/{automation_suite_id}/builds{query}")

    try:
        data = _token_authenticated_request("GET", path)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_WRITE_ADDITIVE)
def submit_automation_build(
    automation_suite_id: int,
    number: str,
    results: JsonObjList | None = None,
    description: str = "",
    branch: str = "",
    start_date: str = "",
    duration: int | None = None,
    test_run_set: JsonObj | None = None,
    external_url: str = "",
    properties: JsonObjList | None = None,
) -> str:
    """
    Submit a new automation build (and optionally its results) to an
    automation suite. Wraps
    POST /{projectID}/automationSuites/{automationSuiteID}/submitBuild.

    This is the primary path for pushing externally-run test results
    (JUnit, xUnit v2, etc.) into Perforce ALM. Parse the XML report on
    the caller side and map each test into a `results` entry — this
    tool does not parse XML itself.

    Args:
        automation_suite_id: Record ID of the suite to submit under.
        number:              Build number (required; free-form string,
                             e.g. "167" or "build-2026.05.20-1").
        results:             Optional list of result dicts. Each item
                             requires `name`, `uniqueName`, and
                             `status` (MenuItem: {"id": <int>} or
                             {"label": "passed"}). Optional per-result
                             fields: device, manufacturer, model, os,
                             osVersion, browser, browserVersion,
                             startDate (ISO 8601), duration (ms),
                             externalURL, properties ([{name, value}]).
        description:         Optional build description.
        branch:              Optional branch name.
        start_date:          Optional ISO 8601 build start timestamp.
        duration:            Optional build duration in milliseconds.
        test_run_set:        Optional MenuItem ({"id"} or {"label"})
                             identifying the test run set.
        external_url:        Optional link to the build in the source
                             system (Jenkins, GitHub Actions, etc.).
        properties:          Optional list of {"name", "value"} dicts
                             to attach to the build as metadata.

    Response: {
      "buildsData": [
        {
          "id": <int>,                 # automation build ID (server-assigned)
          "number": <str>,             # echoed from request
          "submittedDate": <str|null>, # ISO 8601; non-null when results were
                                       # included, null when only metadata
          "self": <str>                # REST href to /builds/{id}
        }
      ]
    }
    The `buildsData` array always has length 1 (this tool creates one build
    per call). Note this endpoint wraps the stub in a `buildsData` container,
    unlike `run_automation_suite` which returns a bare AutomationBuildStub
    without a wrapper. Chain the returned `id` as `build_id` into
    `list_automation_build_results`, `get_automation_build_result`, or
    `associate_automation_results`.
    """
    body: dict = {"number": number}
    if description:
        body["description"] = description
    if branch:
        body["branch"] = branch
    if start_date:
        body["startDate"] = start_date
    if duration is not None:
        body["duration"] = duration
    if test_run_set is not None:
        body["testRunSet"] = test_run_set
    if external_url:
        body["externalURL"] = external_url
    if properties is not None:
        body["properties"] = properties
    if results is not None:
        body["results"] = results

    path = _project_path(f"/automationSuites/{automation_suite_id}/submitBuild")

    try:
        data = _token_authenticated_request("POST", path, body=body)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def list_automation_build_results(
    automation_suite_id: int,
    build_id: int,
    expand: JsonStrList | None = None,
) -> str:
    """
    List automated test results for an automation build. Wraps
    GET /{projectID}/automationSuites/{automationSuiteID}/builds/{buildID}/results.

    Args:
        automation_suite_id: Record ID of the automation suite.
        build_id:            Record ID of the build whose results to
                             list.
        expand:              Optional sub-objects to expand. Allowed
                             values: "testCases", "links".

    Response: {
      "self": <str>,                # REST href to /results
      "resultsData": [
        {
          "id": <int>,              # result ID (chainable)
          "name": <str>,
          "uniqueName": <str>,      # unique within the build
          "status": <MenuItem>,     # {id, label}; label is "passed",
                                    # "failed", "skipped", "blocked", "unknown"
          "self": <str>,            # REST href to /results/{id}
          # plus optional execution metadata (device, manufacturer, model,
          # os, osVersion, browser, browserVersion, startDate, duration,
          # externalURL, properties) and expand-gated sub-objects
          # (testCases, links — request via `expand`)
        }, ...
      ]
    }
    Chain each result's `id` as `result_id` into `get_automation_build_result`,
    or pass a list of result IDs to `associate_automation_results`. The
    endpoint takes no pagination — all results for the build are returned
    at once.
    """
    params: list[tuple[str, str]] = []
    if expand:
        params.extend(("expand", e) for e in expand)

    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    path = _project_path(
        f"/automationSuites/{automation_suite_id}"
        f"/builds/{build_id}/results{query}"
    )

    try:
        data = _token_authenticated_request("GET", path)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_READ_ONLY)
def get_automation_build_result(
    automation_suite_id: int,
    build_id: int,
    result_id: int,
    expand: JsonStrList | None = None,
) -> str:
    """
    Get a single automated test result. Wraps
    GET /{projectID}/automationSuites/{automationSuiteID}/builds/{buildID}/results/{resultID}.

    Args:
        automation_suite_id: Record ID of the automation suite.
        build_id:            Record ID of the build.
        result_id:           Record ID of the result.
        expand:              Optional sub-objects to expand. Allowed
                             values: "testCases", "links".

    Response: {
      "id": <int>,                # result ID
      "name": <str>,
      "uniqueName": <str>,        # unique within the build
      "status": <MenuItem>,       # {id, label}; label is "passed",
                                  # "failed", "skipped", "blocked", "unknown"
      "self": <str>,              # REST href to /results/{id}
      # plus optional execution metadata (device, manufacturer, model,
      # os, osVersion, browser, browserVersion, startDate, duration,
      # externalURL, properties) and expand-gated sub-objects
      # (testCases, links — request via `expand`)
    }
    Unlike `list_automation_build_results`, the response is the
    AutomationResult object directly — there is no top-level `resultsData`
    wrapper. Chain `id` as `result_id` into `associate_automation_results`
    (alongside test case IDs).
    """
    params: list[tuple[str, str]] = []
    if expand:
        params.extend(("expand", e) for e in expand)

    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    path = _project_path(
        f"/automationSuites/{automation_suite_id}"
        f"/builds/{build_id}/results/{result_id}{query}"
    )

    try:
        data = _token_authenticated_request("GET", path)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_WRITE_ASSOCIATIVE)
def associate_automation_results(
    automation_suite_id: int,
    build_id: int,
    result_ids: list[int],
    test_case_ids: list[int],
    add_to_suite: bool = False,
) -> str:
    """
    Associate one or more automated test results with one or more
    test cases. Wraps
    POST /{projectID}/automationSuites/{automationSuiteID}/builds/{buildID}/results/associate.

    Use this when result `uniqueName` / script-ID matching did not
    auto-bind results to their owning test cases at submit time — e.g.
    a test was renamed, or its script ID tag was added after the build
    was already submitted.

    Args:
        automation_suite_id: Record ID of the automation suite.
        build_id:            Record ID of the build.
        result_ids:          Result IDs to associate.
        test_case_ids:       Test case IDs to associate the results
                             with.
        add_to_suite:        If True, also update the suite so this
                             mapping carries forward to future builds
                             (default False — one-time association
                             on this build only).

    Response: {
      "associated": true,
      "automation_suite_id": <int>,     # echoed from input
      "build_id": <int>,                # echoed from input
      "result_ids": [<int>, ...],       # echoed from input
      "test_case_ids": [<int>, ...],    # echoed from input
      "add_to_suite": <bool>            # echoed from input
    }
    Synthesized client-side — the REST endpoint itself returns 201 Created
    with no body. `associated: true` only confirms the POST returned a
    non-error status; failures surface as `"Error: ..."` strings from the
    standard error path. To verify the mapping landed, call
    `get_automation_build_result(... expand=["testCases"])` on the affected
    results.
    """
    body = {
        "resultIDs": result_ids,
        "testCaseIDs": test_case_ids,
        "addToSuite": add_to_suite,
    }
    path = _project_path(
        f"/automationSuites/{automation_suite_id}"
        f"/builds/{build_id}/results/associate"
    )

    try:
        _token_authenticated_request("POST", path, body=body)
    except Exception as e:
        return f"Error: {e}"
    return json.dumps({
        "associated": True,
        "automation_suite_id": automation_suite_id,
        "build_id": build_id,
        "result_ids": result_ids,
        "test_case_ids": test_case_ids,
        "add_to_suite": add_to_suite,
    }, indent=2)


def main() -> None:
    _init_telemetry()
    mcp.run()


if __name__ == "__main__":
    main()

