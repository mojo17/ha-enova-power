"""Guard against dependency-pin drift between the manifest and CI requirements.

GitHub CI installs the library from ``requirements_test.txt`` while local runs
(and the pre-commit hook) use the editable install — so a stale pin there only
ever surfaces on GitHub, after a push. Assert parity here instead, so a
forgotten bump fails at commit time.
"""

from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _manifest_pin() -> str:
    manifest = json.loads(
        (ROOT / "custom_components" / "enova_power" / "manifest.json").read_text()
    )
    (req,) = [r for r in manifest["requirements"] if r.startswith("enovapower")]
    return req


def test_enovapower_pin_matches_test_requirements() -> None:
    lines = (ROOT / "requirements_test.txt").read_text().splitlines()
    (test_req,) = [ln.strip() for ln in lines if ln.strip().startswith("enovapower")]
    assert test_req == _manifest_pin(), (
        "requirements_test.txt and manifest.json pin different enovapower "
        "versions; bump them together"
    )


def test_installed_library_matches_manifest_pin() -> None:
    # The dev venv's (editable) install must match the manifest pin, or the
    # tests exercise a different library than users get — and scripts/develop
    # would make HA try to pull the pinned version from PyPI. After bumping
    # the library version, re-run: pip install -e ~/dev/enovapower
    pinned = _manifest_pin().split("==")[1]
    assert importlib.metadata.version("enovapower") == pinned
