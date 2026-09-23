"""Generic attachment tooling: download_attachment, plus the confinement
helpers shared with the upload_*_attachment family.

Covers the project-segment + encodedFileID percent-encoding done when building
the /files/{encodedFileID} path, the write-to-disk success contract, the
Content-Disposition-derived filename (with fallback and sanitization), and
the confinement of both download destinations and upload sources to their
respective configured root directories.
"""
import json
from unittest.mock import patch

import pytest

import perforce_alm_mcp as alm
from _test_helpers import tool_fn

download_attachment = tool_fn("download_attachment")
upload_requirement_attachment = tool_fn("upload_requirement_attachment")


@pytest.mark.parametrize("name", [
    None,
    "",
    "   ",
    " .. ",
    ".",
    "..",
    "a\x00b",
    "a/b/",
    "/",
    "NUL",
    "con.txt",
    "LPT1",
    "trail.",
])
def test_safe_leaf_name_falls_back_for_untrusted_input(name):
    assert alm._safe_leaf_name(name, "fallback") == "fallback"


def test_safe_leaf_name_reduces_backslash_traversal_to_leaf():
    assert alm._safe_leaf_name("..\\..\\win.ini", "fallback") == "win.ini"


def test_safe_leaf_name_strips_ntfs_alternate_data_stream_suffix():
    assert alm._safe_leaf_name("report.txt:ads", "fallback") == "report.txt"


def test_safe_leaf_name_reduces_drive_prefix_to_leaf():
    assert alm._safe_leaf_name("C:evil.txt", "fallback") == "evil.txt"


def test_safe_leaf_name_passes_through_a_plain_name():
    assert alm._safe_leaf_name("spec.pdf", "fallback") == "spec.pdf"


def test_safe_leaf_name_truncates_long_names_preserving_extension():
    name = "x" * 500 + ".txt"
    leaf = alm._safe_leaf_name(name, "fallback")
    assert leaf.endswith(".txt")
    assert len(leaf) <= alm._MAX_LEAF_NAME


def test_download_attachment_builds_path_and_writes_file(tmp_path, project, monkeypatch):
    monkeypatch.setenv("PERFORCE_ALM_MCP_DOWNLOAD_DIR", str(tmp_path))
    with patch.object(alm, "_download_file_request", return_value=(b"file-bytes", "spec.pdf")) as m:
        out = download_attachment("abc123")
    m.assert_called_once_with("/PROJ/files/abc123")
    destination = tmp_path / "spec.pdf"
    assert destination.read_bytes() == b"file-bytes"
    assert json.loads(out) == {
        "destination_path": str(destination),
        "filename": "spec.pdf",
        "bytes_written": len(b"file-bytes"),
    }


def test_download_attachment_percent_encodes_special_characters(tmp_path, project, monkeypatch):
    monkeypatch.setenv("PERFORCE_ALM_MCP_DOWNLOAD_DIR", str(tmp_path))
    encoded_id = "abc/+=?&"
    with patch.object(alm, "_download_file_request", return_value=(b"x", "out.bin")) as m:
        download_attachment(encoded_id)
    m.assert_called_once_with("/PROJ/files/abc%2F%2B%3D%3F%26")


def test_download_attachment_writes_into_destination_subdir(tmp_path, project, monkeypatch):
    monkeypatch.setenv("PERFORCE_ALM_MCP_DOWNLOAD_DIR", str(tmp_path))
    with patch.object(alm, "_download_file_request", return_value=(b"file-bytes", "spec.pdf")):
        out = download_attachment("abc123", "sub/dir")
    destination = tmp_path / "sub" / "dir" / "spec.pdf"
    assert destination.read_bytes() == b"file-bytes"
    assert json.loads(out)["destination_path"] == str(destination)


def test_download_attachment_request_error_returns_error_string(tmp_path, project, monkeypatch):
    monkeypatch.setenv("PERFORCE_ALM_MCP_DOWNLOAD_DIR", str(tmp_path))
    with patch.object(alm, "_download_file_request", side_effect=RuntimeError("HTTP 404 Not Found: {}")):
        out = download_attachment("abc123")
    assert out == "Error: HTTP 404 Not Found: {}"
    assert list(tmp_path.iterdir()) == []


def test_download_attachment_unwritable_destination_returns_error(tmp_path, project, monkeypatch):
    monkeypatch.setenv("PERFORCE_ALM_MCP_DOWNLOAD_DIR", str(tmp_path))
    # A regular file already occupies the path the tool needs to treat as a directory.
    (tmp_path / "blocker").write_text("not a directory")
    with patch.object(alm, "_download_file_request", return_value=(b"file-bytes", "spec.pdf")):
        out = download_attachment("abc123", "blocker")
    assert out.startswith("Error: ")


def test_download_attachment_rejects_destination_outside_root(tmp_path, project, monkeypatch):
    monkeypatch.setenv("PERFORCE_ALM_MCP_DOWNLOAD_DIR", str(tmp_path))
    with patch.object(alm, "_download_file_request", return_value=(b"file-bytes", "spec.pdf")) as m:
        out = download_attachment("abc123", "../../escape")
    assert out.startswith("Error: ")
    assert "outside the permitted directory" in out
    m.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_download_attachment_sanitizes_malicious_content_disposition_filename(tmp_path, project, monkeypatch):
    monkeypatch.setenv("PERFORCE_ALM_MCP_DOWNLOAD_DIR", str(tmp_path))
    with patch.object(alm, "_download_file_request", return_value=(b"payload", "../../evil.bat")):
        out = download_attachment("abc123")
    assert json.loads(out) == {
        "destination_path": str(tmp_path / "evil.bat"),
        "filename": "evil.bat",
        "bytes_written": len(b"payload"),
    }
    assert (tmp_path / "evil.bat").read_bytes() == b"payload"
    assert not (tmp_path.parent / "evil.bat").exists()


def test_download_attachment_falls_back_to_encoded_file_id_when_no_filename_header(tmp_path, project, monkeypatch):
    monkeypatch.setenv("PERFORCE_ALM_MCP_DOWNLOAD_DIR", str(tmp_path))
    with patch.object(alm, "_download_file_request", return_value=(b"payload", None)):
        out = download_attachment("abc123")
    assert json.loads(out)["filename"] == "abc123"
    assert (tmp_path / "abc123").read_bytes() == b"payload"


def test_download_attachment_refuses_to_overwrite_existing_file_by_default(tmp_path, project, monkeypatch):
    monkeypatch.setenv("PERFORCE_ALM_MCP_DOWNLOAD_DIR", str(tmp_path))
    existing = tmp_path / "spec.pdf"
    existing.write_bytes(b"original-content")
    with patch.object(alm, "_download_file_request", return_value=(b"new-bytes", "spec.pdf")) as m:
        out = download_attachment("abc123")
    assert out.startswith("Error: ")
    assert "already exists" in out
    assert existing.read_bytes() == b"original-content"
    m.assert_called_once()  # the download still happens; only the disk write is blocked


def test_download_attachment_overwrite_true_replaces_existing_file(tmp_path, project, monkeypatch):
    monkeypatch.setenv("PERFORCE_ALM_MCP_DOWNLOAD_DIR", str(tmp_path))
    existing = tmp_path / "spec.pdf"
    existing.write_bytes(b"original-content")
    with patch.object(alm, "_download_file_request", return_value=(b"new-bytes", "spec.pdf")):
        out = download_attachment("abc123", overwrite=True)
    assert existing.read_bytes() == b"new-bytes"
    assert json.loads(out)["bytes_written"] == len(b"new-bytes")


def test_upload_attachment_request_rejects_source_outside_upload_root(tmp_path, project, monkeypatch, non_token_req):
    monkeypatch.setenv("PERFORCE_ALM_MCP_UPLOAD_DIR", str(tmp_path))
    outside = tmp_path.parent / "outside_xyz.bin"
    out = upload_requirement_attachment(7, str(outside))
    assert out.startswith("Error:")
    assert "outside the permitted directory" in out
    non_token_req.assert_not_called()  # the confinement check must fire before any token/network call


def test_upload_attachment_request_reads_file_inside_upload_root(tmp_path, project, monkeypatch):
    monkeypatch.setenv("PERFORCE_ALM_MCP_UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(alm, "_get_token", lambda: "tok")
    source = tmp_path / "spec.pdf"
    source.write_bytes(b"file-bytes")
    with patch.object(
        alm,
        "_execute_request",
        return_value={"attachmentsData": [{"id": 9, "encodedFileID": "abc"}]},
    ) as m:
        out = upload_requirement_attachment(7, str(source))
    assert json.loads(out) == {"attachmentsData": [{"id": 9, "encodedFileID": "abc"}]}
    m.assert_called_once()
