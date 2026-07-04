"""Pure manifest.json checks — no Home Assistant instance required."""

import json
from pathlib import Path

MANIFEST_PATH = (
    Path(__file__).parent.parent
    / "custom_components"
    / "ring_intercom_camera"
    / "manifest.json"
)


def test_manifest_is_valid_json():
    manifest = json.loads(MANIFEST_PATH.read_text())

    assert manifest["domain"] == MANIFEST_PATH.parent.name
    assert "version" in manifest
    assert "documentation" in manifest
    assert "codeowners" in manifest


def test_aiortc_is_never_a_hard_requirement():
    """aiortc's `av` pin can conflict with HA Core's own `av` pin.

    It must stay out of manifest.json requirements — see venv_snapshot.py,
    which installs it into an isolated venv instead.
    """
    manifest = json.loads(MANIFEST_PATH.read_text())
    requirements = " ".join(manifest.get("requirements", [])).lower()

    assert "aiortc" not in requirements
    assert "av==" not in requirements
    assert "av>=" not in requirements
