"""Bearer-token lifecycle (_get_token) and the config-file fallback used to
build _config at import (_load_config_file_env / _env_setting).

These are pure helpers (not @mcp.tool()s), so they're called directly and are
unaffected by fastmcp's decorator_mode.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import perforce_alm_mcp as alm


def _iso(delta):
    return (datetime.now(timezone.utc) + delta).isoformat()


# --- _get_token ---

def test_get_token_returns_cached_when_not_expired():
    alm._config.update(access_token="cached", token_expires_on=_iso(timedelta(hours=1)))
    with patch.object(alm, "_non_token_authenticated_request") as m:
        assert alm._get_token() == "cached"
    m.assert_not_called()


def test_get_token_refreshes_when_expired():
    alm._config.update(access_token="old", token_expires_on=_iso(timedelta(hours=-1)))
    alm._current_project = "PROJ"
    with patch.object(
        alm,
        "_non_token_authenticated_request",
        return_value={"accessToken": "fresh", "expiresOn": _iso(timedelta(hours=1))},
    ) as m:
        assert alm._get_token() == "fresh"
    m.assert_called_once_with("GET", "/PROJ/token")


def test_get_token_raises_without_active_project():
    alm._config.update(access_token="", token_expires_on="")
    alm._current_project = ""
    with pytest.raises(RuntimeError, match="No active project"):
        alm._get_token()


def test_get_token_raises_when_endpoint_returns_no_token():
    alm._config.update(access_token="", token_expires_on="")
    alm._current_project = "PROJ"
    with patch.object(alm, "_non_token_authenticated_request", return_value={}):
        with pytest.raises(RuntimeError, match="did not return accessToken"):
            alm._get_token()


def test_get_token_never_writes_a_refreshed_token_anywhere():
    """A new token is fetched fresh every session and cached in memory only —
    this server has no persistence mechanism for it at all."""
    alm._config.update(access_token="", token_expires_on="")
    alm._current_project = "PROJ"
    with patch.object(
        alm,
        "_non_token_authenticated_request",
        return_value={"accessToken": "fresh", "expiresOn": _iso(timedelta(hours=1))},
    ):
        assert alm._get_token() == "fresh"
    assert alm._config["access_token"] == "fresh"


# --- _load_config_file_env / _env_setting ---

def test_load_config_file_env_returns_empty_when_unset(monkeypatch):
    monkeypatch.delenv(alm._ENV_CONFIG_FILE, raising=False)
    assert alm._load_config_file_env() == {}


def test_load_config_file_env_reads_saved_entry(tmp_path, monkeypatch):
    saved = tmp_path / "saved.json"
    saved.write_text(json.dumps({
        "mcpServers": {alm._SERVER_NAME: {"env": {alm._ENV_URL: "saved-host"}}}
    }))
    monkeypatch.setenv(alm._ENV_CONFIG_FILE, str(saved))
    assert alm._load_config_file_env() == {alm._ENV_URL: "saved-host"}


def test_load_config_file_env_missing_file_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv(alm._ENV_CONFIG_FILE, str(tmp_path / "does-not-exist.json"))
    assert alm._load_config_file_env() == {}


def test_load_config_file_env_malformed_json_returns_empty(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    monkeypatch.setenv(alm._ENV_CONFIG_FILE, str(bad))
    assert alm._load_config_file_env() == {}


def test_load_config_file_env_ignores_other_server_names(tmp_path, monkeypatch):
    saved = tmp_path / "saved.json"
    saved.write_text(json.dumps({
        "mcpServers": {"Some Other Server": {"env": {alm._ENV_URL: "not-ours"}}}
    }))
    monkeypatch.setenv(alm._ENV_CONFIG_FILE, str(saved))
    assert alm._load_config_file_env() == {}


def test_env_setting_direct_env_var_wins_over_file(monkeypatch):
    monkeypatch.setenv(alm._ENV_URL, "direct-host")
    monkeypatch.setattr(alm, "_file_env", {alm._ENV_URL: "file-host"})
    assert alm._env_setting(alm._ENV_URL) == "direct-host"


def test_env_setting_falls_back_to_file_when_env_var_unset(monkeypatch):
    monkeypatch.delenv(alm._ENV_URL, raising=False)
    monkeypatch.setattr(alm, "_file_env", {alm._ENV_URL: "file-host"})
    assert alm._env_setting(alm._ENV_URL) == "file-host"


def test_env_setting_falls_back_to_default_when_neither_set(monkeypatch):
    monkeypatch.delenv(alm._ENV_URL, raising=False)
    monkeypatch.setattr(alm, "_file_env", {})
    assert alm._env_setting(alm._ENV_URL, "default-host") == "default-host"
