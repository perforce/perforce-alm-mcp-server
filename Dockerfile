# Digest-pinned for reproducible, supply-chain-safe builds (multi-arch index
# digest, so multi-platform builds still resolve). Refresh the digest when
# intentionally moving to a newer 3.13-slim:
#   docker buildx imagetools inspect python:3.13-slim
FROM python:3.13-slim@sha256:c33f0bc4364a6881bed1ec0cc2665e6c53c87a43e774aaeab88e6f17af105e4f

LABEL io.modelcontextprotocol.server.name="io.github.perforce/perforce-alm-mcp-server"

# Create the non-root user the process runs as. This server never writes
# configuration to disk itself — config comes from env vars, or from a file
# mounted read-only and pointed at via PERFORCE_ALM_MCP_CONFIG_FILE (see
# README's "Persisting settings across restarts") — so mcpuser needs no
# write access anywhere under /app.
# Note: the command/args in the entry this server builds (returned by
# export_mcp_entry) default to `python /app/perforce_alm_mcp.py` —
# valid inside the container, broken on the host. If you save that entry to
# register the server elsewhere, set PERFORCE_ALM_MCP_LAUNCH_COMMAND=docker and
# PERFORCE_ALM_MCP_LAUNCH_ARGS (a JSON array) to your `docker run …` invocation.
RUN useradd -u 1000 -m -s /usr/sbin/nologin mcpuser

WORKDIR /app

# Install dependencies from requirements.txt with --require-hashes: every
# package and version is exactly what was resolved into uv.lock, and pip
# refuses to proceed if PyPI serves bytes that don't match the recorded
# hash. Regenerate requirements.txt (see CLAUDE.md's Dependencies section)
# whenever pyproject.toml's dependencies change - a stale file here would
# silently keep installing old versions, not fail loudly.
# README.md is required because pyproject's `readme = "README.md"` is read to
# build the package metadata. Files are left root-owned (no --chown): mcpuser
# can read and run perforce_alm_mcp.py but not overwrite it, preventing the
# runtime process from tampering with its own server module.
COPY pyproject.toml perforce_alm_mcp.py README.md requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt && \
    pip install --no-cache-dir --no-deps .

USER mcpuser

# MCP over stdio — the client launches the container via `docker run -i --rm ...`.
ENTRYPOINT ["python", "perforce_alm_mcp.py"]
