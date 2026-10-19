"""The vendored cipherweave_verify is byte-for-byte the pinned CipherWeave commit.

It is a copy of security code. A local edit would make this Weave accept
something the other three refuse, so any change has to come from CipherWeave
through scripts/sync_cipherweave_verify.sh, which rewrites the manifest.
"""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "cipherweave_verify"


def test_vendored_files_match_their_manifest():
    manifest = json.loads((PACKAGE / "VENDORED.json").read_text())
    actual = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(PACKAGE.glob("*.py"))}
    assert actual == manifest["sha256"], "edit CipherWeave and re-run scripts/sync_cipherweave_verify.sh"


def test_manifest_names_the_pinned_commit():
    manifest = json.loads((PACKAGE / "VENDORED.json").read_text())
    pin = next(
        line.strip()
        for line in (ROOT / "requirements-cipherweave.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    )
    assert manifest["source"] == pin
    commit = pin.split("@")[-1].split("#")[0]
    assert len(commit) == 40 and all(c in "0123456789abcdef" for c in commit)
