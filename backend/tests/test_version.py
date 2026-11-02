"""One version for everything: the VERSION file, every manifest that names the project, and the
images that run backend code (backend/version.py reads VERSION at import)."""

import json
import re
import tomllib
from pathlib import Path

from backend.version import VERSION

ROOT = Path(__file__).resolve().parents[2]


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSION), VERSION


def test_every_manifest_carries_the_version():
    found = {
        "pyproject.toml": tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"],
        "python/pyproject.toml": tomllib.loads((ROOT / "python/pyproject.toml").read_text())["project"]["version"],
        "frontend/package.json": json.loads((ROOT / "frontend/package.json").read_text())["version"],
    }
    lock = json.loads((ROOT / "frontend/package-lock.json").read_text())
    found["frontend/package-lock.json"] = lock["version"]
    found["frontend/package-lock.json (root package)"] = lock["packages"][""]["version"]
    uv_lock = tomllib.loads((ROOT / "uv.lock").read_text())
    for package in uv_lock["package"]:
        if package["name"] in ("freedomridge", "freedomridge-app"):
            found[f"uv.lock ({package['name']})"] = package["version"]
    assert len(found) == 7
    assert {name: version for name, version in found.items() if version != VERSION} == {}


def test_images_that_run_backend_code_ship_the_version_file():
    for dockerfile in sorted(ROOT.glob("Dockerfile.*")):
        text = dockerfile.read_text()
        if re.search(r"^COPY backend ", text, re.M):
            assert re.search(r"^COPY VERSION ", text, re.M), f"{dockerfile.name} copies backend/ but not VERSION"
