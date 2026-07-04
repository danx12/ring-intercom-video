"""Unit tests for the isolated snapshot-venv manager (no aiortc/av needed)."""

import sys
from pathlib import Path

from custom_components.ring_intercom_camera import venv_snapshot


def test_venv_python_path_posix(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert venv_snapshot._venv_python("/config") == Path(
        "/config/ring_intercom_camera_venv/bin/python"
    )


def test_venv_python_path_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert venv_snapshot._venv_python("/config") == Path(
        "/config/ring_intercom_camera_venv/Scripts/python.exe"
    )


async def test_capture_returns_none_when_venv_missing(tmp_path):
    """No subprocess should even be spawned if the venv was never set up."""
    result = await venv_snapshot.capture(str(tmp_path), "wss://example.invalid", 123)
    assert result is None
