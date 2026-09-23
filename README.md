# Perforce ALM MCP Server

The Perforce ALM MCP Server wraps the Perforce ALM REST API and exposes it to MCP-compatible AI clients, such as Claude Code, Claude Desktop, Cursor, Codex, and others.

## Features

- Tools for requirements, requirement documents, document trees and snapshots, issues, test cases (including steps and links), automation suites and build results, and menu configurations
- Supports API key and basic (username/password) authentication
- Single-file design: `perforce_alm_mcp.py` contains the entire server
- Built on the `fastmcp` framework. REST API calls use the Python standard library (`urllib`).
- Includes OpenTelemetry tracing over OTLP/gRPC. Tracing is disabled during development and remains inactive until enabled in the source. The Perforce Agentic Gateway can redirect the telemetry destination by using standard OpenTelemetry environment variables.

## Requirements

- Python 3.13 or later
- Perforce ALM 2026.1 or later. Older versions may work but are not tested.
- Network access to a Perforce ALM REST API installation
- An MCP-compatible AI client

## Installation

```
git clone <repo-url>
cd "ALM MCP Server"
pip install .
```

Verify that the server imports successfully:

```
python -c "import perforce_alm_mcp"
```

## Supported deployment methods

The server communicates over MCP using **stdio** and is started by your MCP client, not run as a standalone service. The client starts the server on demand by running `python perforce_alm_mcp.py`. Because the process waits for input on stdin, do not run it manually. The server works with any stdio-capable MCP client, including:

- **Claude Code**: Reads a project's `.mcp.json` file automatically to determine how to start the server. Register the server in `.mcp.json` (see **Client configuration** below) or by using `claude mcp add`. The server does not write to `.mcp.json`. For information about saving settings to this file, see **Persisting settings across restarts** in [Environment variables](#environment-variables).
- **Claude Desktop, Cursor, Codex, and other MCP clients**: Register the server in the client's MCP configuration. Use the `export_mcp_entry` tool to generate a configuration snippet. The API key ID and secret are redacted from the output. Provide the actual values when prompted.

To run multiple installations side by side, assign each installation a different name by using `PERFORCE_ALM_MCP_SERVER_NAME`. For more information, see [Environment variables](#environment-variables).

## Client configuration

To register the server, add the following entry to your client's MCP configuration:

```json
{
  "mcpServers": {
    "Perforce ALM": {
      "command": "python",
      "args": ["C:/path/to/perforce_alm_mcp.py"]
    }
  }
}
```

- **Claude Code**: Save this configuration as `.mcp.json` in the project directory.
- **Claude Desktop, Cursor, Codex, and other MCP clients**: Add the configuration to the client's MCP configuration file.

After the initial connection, you can use the `export_mcp_entry` tool to regenerate this configuration snippet. The API key ID and secret are redacted from the output. Provide those values when prompted.

You can also register the server from the Claude Code CLI by using a single command (replace the path as needed):

```powershell
claude mcp add "Perforce ALM" -- python "C:/path/to/perforce_alm_mcp.py"
```

This command creates the same entry in `.mcp.json`.

## Run with uvx

The server is packaged in `pyproject.toml` with a console-script entry point, so you can run it directly with [uv](https://docs.astral.sh/uv/)'s `uvx`. No manual `pip install` command or local checkout is required. On startup, `uv` resolves the dependencies exactly pinned in `pyproject.toml` into an ephemeral environment.

**From PyPI:**

Once published as `perforce-alm-mcp`:


  ```
  uvx perforce-alm-mcp
  ```

**From a Git repository:** 

No PyPI publish required.

  ```
  uvx --from git+https://github.com/perforce/perforce-alm-mcp-server perforce-alm-mcp
  ```

  To use a specific tag, branch, or commit, append `@<ref>` to the repository URL. For example: `git+https://github.com/perforce/perforce-alm-mcp-server@v0.19.0`

**MCP client configuration (PyPI form):**

```json
{
  "mcpServers": {
    "Perforce ALM": {
      "command": "uvx",
      "args": ["perforce-alm-mcp"],
      "env": {
        "PERFORCE_ALM_URL": "alm.example.com",
        "PERFORCE_ALM_PORT": "8443",
        "PERFORCE_ALM_AUTH_TYPE": "apikey",
        "PERFORCE_ALM_API_KEY_ID": "...",
        "PERFORCE_ALM_API_KEY_SECRET": "..."
      }
    }
  }
}
```

For a Git-based installation, use:

`"args": ["--from", "git+https://github.com/perforce/perforce-alm-mcp-server", "perforce-alm-mcp"]`

>**Important:** When you run the server through `uvx`, configure it by using environment variables. The server does not persist configuration to disk, regardless of the deployment method. Provide connection settings through the client's `env` block, as shown above, or set `PERFORCE_ALM_MCP_CONFIG_FILE` to a configuration file. For more information, see **Persisting settings across restarts** in [Environment variables](#environment-variables). Basic authentication credentials are session-specific. Set `PERFORCE_ALM_USERNAME` and `PERFORCE_ALM_PASSWORD` as environment variables, or call `set_basic_credentials` at the start of each session.

### Publish to PyPI

To enable the simplified `uvx perforce-alm-mcp` form, build and publish the package from the repository root:

```
uv build
uv publish
```

The package version is read automatically from `_MCP_VERSION` in `perforce_alm_mcp.py`. This ensures that the published package version always matches the version reported by `get_versions`.

## Run with Docker

A `Dockerfile` is included. It uses a Python 3.13 slim base image, runs as a non-root `mcpuser`, and installs dependencies with `pip install --require-hashes` against the hashes recorded in `requirements.txt`.

Build the image:

```
docker build -t perforce-alm-mcp .
```

Like the other deployment methods, the server communicates over **stdio**, so the client launches a container for each session by using `docker run -i --rm`. Include `-i` for `stdin` and **do not** use `-t`. Pass configuration settings through `-e` flags:

```json
{
  "mcpServers": {
    "Perforce ALM": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "PERFORCE_ALM_URL",
        "-e", "PERFORCE_ALM_PORT",
        "-e", "PERFORCE_ALM_AUTH_TYPE",
        "-e", "PERFORCE_ALM_API_KEY_ID",
        "-e", "PERFORCE_ALM_API_KEY_SECRET",
        "-e", "PERFORCE_ALM_SSL_VERIFY",
        "perforce-alm-mcp"
      ],
      "env": {
        "PERFORCE_ALM_URL": "alm.example.com",
        "PERFORCE_ALM_PORT": "8443",
        "PERFORCE_ALM_AUTH_TYPE": "apikey",
        "PERFORCE_ALM_API_KEY_ID": "...",
        "PERFORCE_ALM_API_KEY_SECRET": "...",
        "PERFORCE_ALM_SSL_VERIFY": "true"
      }
    }
  }
}
```

A bare `-e VAR` forwards the value from the `env` block into the container.

### Docker-specific notes

#### Configure by using environment variables
The server does not persist configuration to disk. Because the container runs with `--rm`, it is also ephemeral. Pass settings through `-e` flags, as shown above, or save a configuration file to the host, bind-mount that file as read-only, and set `PERFORCE_ALM_MCP_CONFIG_FILE` to the in-container path. For example:
`-v "$(pwd)/alm-config.json:/config/alm-config.json:ro" -e PERFORCE_ALM_MCP_CONFIG_FILE=/config/alm-config.json`.

**Do not** mount anything at `/app`. A bind mount at that location hides the packaged `perforce_alm_mcp.py` file and prevents the container from starting (`python: can't open file '/app/perforce_alm_mcp.py'`). A named volume at `/app` can also become outdated when the image is updated.

The bearer token is never persisted. The server retrieves a new token for each session.

#### Host-launchable configuration entry
Inside the container, `__file__` is `/app/perforce_alm_mcp.py`. As a result, the `command` and `args` values generated by `export_mcp_entry` default to `python /app/perforce_alm_mcp.py`. This launch command is valid inside the container but not on the host.

If you save the generated entry for use outside the container, set `PERFORCE_ALM_MCP_LAUNCH_COMMAND=docker` and set `PERFORCE_ALM_MCP_LAUNCH_ARGS` to a JSON array that matches the host-side Docker launch command. For example:
```
["run","-i","--rm","-e","PERFORCE_ALM_URL",...,"perforce-alm-mcp"]
```
The image cannot provide these values automatically because it does not know which image tag, mounts, or environment variables you plan to use.

#### Networking

`localhost` inside the container refers to the container itself, not the host system. If the ALM REST API runs on the host computer, set `PERFORCE_ALM_URL` to `host.docker.internal` (Docker Desktop on Windows or macOS), use `--network host` (Linux), or specify the actual hostname or IP address.

#### TLS
For a self-signed certificate, set `PERFORCE_ALM_SSL_VERIFY=false`.

#### Basic authentication

Basic authentication is session-specific. Set `PERFORCE_ALM_USERNAME` and `PERFORCE_ALM_PASSWORD` through `-e` flags, or call `set_basic_credentials` at the start of each session.

## Environment variables

You can provide every setting through an environment variable in the MCP client's `env` block or in the shell environment before startup. The server does not write these settings to disk. For information about preserving settings across restarts, see **Persisting settings across restarts** below.

| Variable | Purpose |
|---|---|
| `PERFORCE_ALM_URL` | Perforce ALM REST API hostname or IP address. The protocol is optional and defaults to `https://`. |
| `PERFORCE_ALM_PORT` | REST API port (for example, `8443`) |
| `PERFORCE_ALM_AUTH_TYPE` | Authentication type: `apikey` or `basic` |
| `PERFORCE_ALM_API_KEY_ID` | API key ID (for `apikey` authentication) |
| `PERFORCE_ALM_API_KEY_SECRET` | API key secret (for `apikey` authentication) |
| `PERFORCE_ALM_USERNAME` | Username (for `basic` authentication). This value is always session-specific. See **Authentication notes** below. |
| `PERFORCE_ALM_PASSWORD` | Password (for `basic` authentication). This value is always session-specific. See **Authentication notes** below. |
| `PERFORCE_ALM_SSL_VERIFY` | Set to `false` to disable TLS cert verification. Any other value is treated as `true` (default). |
| `PERFORCE_ALM_DEFAULT_PROJECT` | Project ID to activate when the server starts |
| `PERFORCE_ALM_MCP_CONFIG_FILE` | Path to a previously saved configuration file. See **Persisting settings across restarts** below. Values from this file are used only when the corresponding environment variable is not already set. |
| `PERFORCE_ALM_MCP_SERVER_NAME` | Display name and `mcpServers` key used when the server generates an MCP configuration entry. The default value is `Perforce ALM`. Use this setting if you need multiple installations to coexist. |
| `PERFORCE_ALM_MCP_LAUNCH_COMMAND` | Overrides the `command` value in MCP configuration entries generated by the server. The default value is `python`. Use this setting when the launch command differs from the in-process path, such as in Docker or `uvx` deployments. |
| `PERFORCE_ALM_MCP_LAUNCH_ARGS` | Overrides the `args` value in MCP configuration entries generated by the server. Specify the value as a JSON array. The default value is `["<this script's path>"]`. Use with `PERFORCE_ALM_MCP_LAUNCH_COMMAND` for Docker or `uvx` deployments. |
| `PERFORCE_ALM_MCP_DOWNLOAD_DIR` | Directory that `download_attachment` can write files to. Downloads are restricted to this directory. The default location is a folder under the system temporary directory. See **Known limitations** below. |
| `PERFORCE_ALM_MCP_UPLOAD_DIR` | Directory that `upload_*_attachment` can read files from. Uploads are restricted to this directory. The default location is a folder under the system temporary directory. See **Known limitations** below. |

## Initial server configuration

When you connect to the server for the first time, ask your AI client to "configure Perforce ALM." The client prompts you for the following information:

| Value | Description |
|---|---|
| `REST API URL` | Hostname or IP address of the REST API endpoint, not the ALM Server. The protocol is optional and defaults to `https://`. |
| `Port` | REST API port (for example, `8443`) |
| `Auth type` | `apikey` or `basic` |
| `Credentials` | API key ID and secret, or username and password |
| `SSL verify` | Defaults to `true` |

These settings apply only after the server restarts. The server does not write configuration data to disk. To preserve settings across restarts, see the next section.

### Persisting settings across restarts

Because these settings are environment variables in the MCP client's own configuration, they persist across restarts by default. The client sets them each time it starts the server. The server reads them automatically. The server does not write this configuration to disk automatically.

If you prefer not to store individual environment variables in the client configuration, save the settings to a separate file and set `PERFORCE_ALM_MCP_CONFIG_FILE=<path-to-file>` in the client configuration.

When `PERFORCE_ALM_MCP_CONFIG_FILE` is set, the server reads configuration values from that file during startup. Values from the file are used only when the corresponding `PERFORCE_ALM_*` environment variable is not already set. Directly defined environment variables always take precedence.

### Authentication notes

#### API key
The API key ID and secret are stored in the `PERFORCE_ALM_API_KEY_ID` and `PERFORCE_ALM_API_KEY_SECRET` environment variables. The server does not write these values to disk.

To rotate an API key, set those environment variables (in the client configuration or the `PERFORCE_ALM_MCP_CONFIG_FILE` file) to the new key ID and secret. After rotating, restart the server.

#### Basic authentication (username/password)
These credentials are always session-specific. The username and password are never included in `export_mcp_entry`'s output, and the server does not write them to disk. If you choose to include these values in your own `PERFORCE_ALM_MCP_CONFIG_FILE`, the server treats them the same as values provided through `PERFORCE_ALM_USERNAME` and `PERFORCE_ALM_PASSWORD`. The server does not generate or save these values for you.

Only the username is required. The password can be empty if the account is configured without one.

At the start of each session, either:

- Call `set_basic_credentials` to provide credentials at runtime
or
- Set `PERFORCE_ALM_USERNAME` and `PERFORCE_ALM_PASSWORD` as shell environment variables before the MCP client starts

  `set_basic_credentials` verifies the credentials by calling `GET /projects` before storing them. If verification fails, such as with a `401` or `403` response, the command returns an error and leaves the existing credentials unchanged. Invalid credentials never replace working credentials.

A project-scoped bearer token is retrieved at the start of each session and stored only in memory for the lifetime of that session. The server automatically refreshes the token when it expires. The token is never written to disk.

If the ALM Server is restarted and the cached token is no longer valid, call `refresh_token` to retrieve a new token.

## Security

Create a dedicated Perforce ALM user account for the MCP server credentials instead of using a personal account. Add this user to a security group that grants only the permissions required by the AI client. This approach limits the MCP server's access to a defined subset of your Perforce ALM data and reduces the risk of unintended access.

To learn more, see the "Add separate users and use API keys for REST API authentication" section in the ALM [Security best practices](https://help.perforce.com/helix-alm/helixalm/current/client/Content/SharedGlobal/SecurityBestPractices.htm#add-separate-users-and-use-api-keys-for-rest-api-authentication).

The security group assigned to the account determines which write operations succeed, regardless of which tools the AI client calls. Perforce ALM security groups grant permissions by item type and action, so access is not limited to an all-or-nothing choice between read and write permissions. For example:

- **Read-only access.** Grant view permissions only, with no add, edit, or delete permissions on any item type. In this configuration, all `create_*`, `update_*`, and `upload_*_attachment` tools fail at the ALM Server.
- **Write access limited to specific item types.** For example, grant edit permissions on requirements and test cases, but not issues. In this configuration, `update_requirements` and `update_testcases` continue to work, and `update_issues` fails at the ALM Server.
- **No automation execution.** Withhold permissions to run or submit automation suite builds while granting other write permissions. In this configuration, `run_automation_suite` and `submit_automation_build` fail, and other write tools continue to work.

This restriction is enforced by the ALM Server, not by this MCP server, so they apply regardless of which tool the AI client calls.

Dependencies in `pyproject.toml` are pinned to exact versions rather than minimum versions. `pip install .` and `uvx` resolve dependencies directly from this file. The Docker image resolves dependencies from `requirements.txt`, which is generated from `pyproject.toml`. Using minimum-version constraints would allow installations to pick up newer package versions as they become available on PyPI. The Docker image also verifies installed packages against the hashes recorded in `requirements.txt`.

## Per-session bootstrap

Most tools require an active project. The expected flow is:

1. Call `get_active_project` to view the current project.
2. If no project is active, call `list_projects` to view available projects. Then, call `select_project` with the project ID to use.

If `auth_type` is `basic` and credentials have not been provided, call `set_basic_credentials` before using other tools.

**Projects still loading**

The ALM Server can return a project list before all projects are fully loaded. The `GET /projects` response includes a `projectsLoading` value that indicates how many projects are not yet ready.

The server exposes this information in several places:

- `list_projects` includes a `projects_loading_note`
- `select_project` includes the information in its error message when the requested project ID is not yet available

If an expected project is missing, wait a short time and try again before assuming that the project does not exist.

## Available tools

The MCP server provides tools for working with requirements, issues, documents, test cases, automation suites, build results, and related Perforce ALM data.

| Category | Tools |
|---|---|
| Bootstrap / session | `set_basic_credentials`, `list_projects`, `select_project`, `get_active_project`, `refresh_token`, `export_mcp_entry`, `get_versions` |
| Requirements | `get_requirement`, `get_requirements_by_query`, `create_requirements`, `update_requirements`, `list_requirement_links`, `create_requirement_links`, `list_requirement_attachments`, `upload_requirement_attachment` |
| Issues | `get_issue`, `get_issues_by_query`, `create_issues`, `update_issues`, `list_issue_links`, `create_issue_links`, `list_issue_attachments`, `upload_issue_found_by_attachment` |
| Documents | `get_document`, `get_documents_by_query`, `create_documents`, `update_documents`, `list_document_links`, `create_document_links`, `list_document_attachments`, `upload_document_attachment` |
| Document trees | `get_document_tree`, `add_document_tree_nodes` |
| Document snapshots | `list_document_snapshots`, `create_document_snapshot` |
| Test cases | `get_testcase`, `get_testcases_by_query`, `create_testcases`, `update_testcases`, `list_testcase_steps`, `update_testcase_steps`, `list_testcase_links`, `create_testcase_links`, `list_testcase_attachments`, `upload_testcase_attachment` |
| Attachments | `download_attachment` |
| Automation suites | `list_automation_suites`, `get_automation_suite`, `create_automation_suites`, `update_automation_suite`, `list_automation_suite_testcases`, `add_automation_suite_testcases`, `remove_automation_suite_testcase`, `run_automation_suite`, `list_automation_suite_builds` |
| Automation build results | `submit_automation_build`, `list_automation_build_results`, `get_automation_build_result`, `associate_automation_results` |
| Menus (configs) | `list_menus`, `list_menu_items`, `list_menu_fields` |

Tool parameters and response formats are documented in the docstrings in `perforce_alm_mcp.py`.

Use `get_versions` to view both the MCP server version and the version of the connected ALM REST API.

The ALM OpenAPI specification included with the REST API is the authoritative source for REST API behavior and data contracts.

## Search expression syntax

Tools ending in `_by_query` accept an ALM search expression:

```
Product = 'WysiCorp' and (Summary contains 'login' or Description contains 'auth')
```

- Field names must match an existing field label. Matching is **case-insensitive** (`Summary`, `summary`, and `SUMMARY` are equivalent), but a misspelled or nonexistent field returns a `404` error.
- Multi-word field labels must be enclosed in **double quotation marks** in raw expressions. For example, `"Multi Word Field" = 'X'`. A multi-word field label without quotation marks returns `400 Bad Request: "Incorrect syntax starting at '<first-word>'"`.
- String literals can be enclosed in either single or double quotation marks. Search expressions do not support escaping quotation marks within string literals, and doubled quotation marks (`''`) are not supported. To include a quotation mark in a value, use the other quotation style:
  - Use `"Value's"` for a value that contains `'`
  - Use `'JSON: "{...}"'` for a value that contains `"`

The `filters` parameter provides equivalent functionality. Each `{label: value}` pair is converted to `"label" = 'value'`, and multiple expressions are joined with `and`.

Because `filters` automatically encloses field labels in double quotation marks, it works with multi-word field labels without requiring manual quoting. Use `filters` when field labels contain spaces to avoid syntax errors.

For a complete description of the search syntax, see the [ALM REST API documentation](https://help.perforce.com/helix-alm/helixalm/current/restapi/Content/RESTAPI/LimitingItemsReturned.htm).

## Telemetry

The MCP server includes OpenTelemetry instrumentation for tool calls and follows the MCP OpenTelemetry semantic conventions. Collected telemetry includes tool name, method, duration, client name, client version, and success or failure status.

The server **does not** record tool arguments, tool results, or error messages. It records only metadata about each tool call. Traces are exported over OTLP/gRPC.

Telemetry is controlled by Perforce and is currently disabled for development. When telemetry is disabled, no exporter is loaded and tracing introduces no runtime overhead.

When telemetry is enabled in a released version, traces are exported to a Perforce-operated OTLP/gRPC endpoint by default. You can override the destination at runtime by using standard OpenTelemetry environment variables such as `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` or `OTEL_EXPORTER_OTLP_ENDPOINT`.

This configuration is useful when the server runs behind the Perforce Agentic Gateway or another telemetry collection service. If these variables are not set, the server uses the compiled default endpoint.

## Known limitations

- **Workflow transitions are not exposed.** The REST API provides information about workflow events that have already been applied to items.
- **Link editing is not exposed for any resource type** (requirements, issues, test cases, or documents). You can create links by using `create_*_links` and view links by using `list_*_links`, but you cannot edit or remove existing links.
- **Test runs and folders are not yet available as MCP tools** but are available through the REST API.
- **Attachment uploads are supported for requirements, test cases, and documents** through `upload_*_attachment` (POST). This is the only supported way to add attachments.
- **Issues do not provide an item-level attachment endpoint.** To attach a file to an issue, use `upload_issue_found_by_attachment`, which adds the file to one of the issue's found by records. Issue attachments are stored in found by records or workflow events. The `create_requirements`, `create_testcases`, `create_documents`, and `create_issues` tools do not include an `attachments` parameter. The corresponding REST API create (`POST`) endpoints ignore attachment payloads and return success without creating attachments. The endpoints handle inline links in the same way. Any attachment can be downloaded by using the `download_attachment` tool, which writes the file to disk on the computer running the server.
- **Attachment reads and writes are restricted to server-side directories.** `download_attachment` can write files only within the directory specified by `PERFORCE_ALM_MCP_DOWNLOAD_DIR`. `upload_*_attachment` can read files only from the directory specified by `PERFORCE_ALM_MCP_UPLOAD_DIR`. By default, both locations are folders under the system temporary directory. Paths that resolve outside the configured directory are rejected.
- **Attachment tools require access to the configured upload and download directories.** `download_attachment` and `upload_*_attachment` read and write files on the computer where the server is running, not on the caller's device. When the server runs locally, the configured directories are typically available to the user. When the server runs remotely, such as behind the Perforce Agentic Gateway or in a Docker container on another host, the configured directories exist on that remote system instead. Tool responses do not include raw file contents, so this server does not provide a way to transfer files between the remote directory and the user's device. Use an external file transfer method, such as a mounted network share or `scp`, when access to those files is required.
- **Attachments cannot be edited or removed.** The corresponding REST API update (`PUT`) endpoints ignore attachment payloads without returning an error. To prevent changes that do not take effect, the `update_requirements`, `update_testcases`, `update_documents`, and `update_issues` tools reject an `attachments` field. You can embed new inline images in a formatted-string field by including a base64-encoded `<img>` tag in the value passed through a `create_*` or `update_*` tool's `fields` argument. However, no tool currently documents this format directly.
- **Document tree node updates are not exposed.** You can add document tree nodes, but you cannot modify existing nodes.
- **Query searches do not support workflow event fields.**
- **Renamed fields are not supported.** All tools reference fields by their current label (for example, `Summary`, `Description`, or `Product`). Field matching is case-insensitive, but tools do not support renamed fields. If a project administrator renames a field in Perforce ALM, any `search`, `filters`, or `fields` argument, or any `create_*` or `update_*` operation that still references the old label, fails. Typical failures return a `404` error indicating that the field does not exist. The `list_menu_fields` tool is the only thing that exposes a field's stable field name alongside its label, and only for fields that are backed by a menu. Issues provide a partial exception. The Description, Date Found, Version Found, Steps to Reproduce, Reproducible, Test Config, Other Hardware and Software, and Found By fields can also be set through the `found_by_records` parameter in `create_issues` and `update_issues`. These values use fixed JSON schema keys instead of field labels and continue to work after a field rename.

## Troubleshooting

### "The Perforce ALM License Server prevented this action" error

Tools that require the license server to validate the API key, such as `list_projects`, fail with an error that includes `The Perforce ALM License Server prevented this action`. Tools that do not require the license server to validate the API key, such as `get_versions`, continue to succeed.

This issue occurs only when using API key authentication and indicates that the configured API Key ID or Secret is invalid. This can happen if the value is incorrect or if the API key was deleted and is no longer recognized by the license server.

The MCP server does not generate this error. It passes through the message returned by the ALM REST API, which is reporting a license server authentication failure. Because most tool calls require a bearer token, either to obtain a new token or to refresh an expired one, this error can occur during almost any tool call and is not limited to the tool that triggered the token request.

On the license server, this failure generates an Unusual Activity server log entry similar to:
`A login attempt from [indeterminate] at ::1 failed: invalid API key secret.`

The log message always reports invalid API key secret, even when the invalid value is the API Key ID rather than the secret.

To resolve the issue, remove the API key from your MCP client configuration, add the correct API key (or generate and add a new one), and restart the MCP client, such as Claude Desktop or Claude Code, so that it loads the updated configuration.

## Development

See `CLAUDE.md` for architecture notes and conventions when extending the server.

Verify that the server imports successfully:

```
python -c "import perforce_alm_mcp"
```

No linting configuration is currently included.

To verify that the package builds successfully, run `uv build`. You can also build a wheel by using `pip wheel .`. For package publishing flow, see [Run with uvx](#run-with-uvx). For the image build, see [Run with Docker](#run-with-docker).

Validate behavior changes by testing tools through an MCP client.

## License

Licensed under the MIT License. Copyright © 2026 Perforce Software, Inc. See [LICENSE.txt](LICENSE.txt) for the full license.
