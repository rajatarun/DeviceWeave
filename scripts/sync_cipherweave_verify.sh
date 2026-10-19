#!/usr/bin/env bash
# Refresh the vendored src/cipherweave_verify from the CipherWeave commit pinned
# in requirements-cipherweave.txt, and rewrite its checksum manifest.
#
# Vendored, not installed at build time: CipherWeave is private, and a deploy
# that must fetch it needs a GitHub credential in CI (and, for sam build
# --use-container, inside the build container). A copy pinned by commit and
# guarded by tests/test_cipherweave_vendored.py needs neither.
#
# Needs read access to rajatarun/CipherWeave on the machine running it.
set -euo pipefail
cd "$(dirname "$0")/.."
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
python3 -m pip install --quiet --no-deps --target "$tmp" -r requirements-cipherweave.txt
rm -rf src/cipherweave_verify
cp -r "$tmp/cipherweave_verify" src/cipherweave_verify
find src/cipherweave_verify -name __pycache__ -prune -exec rm -rf {} +
python3 - <<'PY'
import hashlib, json, pathlib
root = pathlib.Path("src/cipherweave_verify")
pin = next(l.strip() for l in pathlib.Path("requirements-cipherweave.txt").read_text().splitlines()
           if l.strip() and not l.lstrip().startswith("#"))
files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob("*.py"))}
(root / "VENDORED.json").write_text(json.dumps({"source": pin, "sha256": files}, indent=2) + "\n")
print(f"vendored {len(files)} files from {pin}")
PY
