"""Path-security tests for safe_output_path (agent.tool_registry).

Note: written against the real Windows behaviour of the function:
* a leading forward slash is stripped before the absolute-path check, so the
  absolute-path test uses a drive-qualified path (C:\\foo.xlsx).
"""

from __future__ import annotations

import pytest

from agent.tool_registry import safe_output_path


def test_accepts_valid():
    result = safe_output_path("report.xlsx")
    assert result.endswith("report.xlsx")


def test_rejects_absolute_path():
    with pytest.raises(ValueError, match="Absolute paths"):
        safe_output_path(r"C:\foo.xlsx")


def test_rejects_traversal():
    with pytest.raises(ValueError, match="Path traversal"):
        safe_output_path("../../etc/passwd.xlsx")


def test_rejects_disallowed_extension():
    with pytest.raises(ValueError, match="Disallowed"):
        safe_output_path("malware.exe")


def test_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        safe_output_path("")
