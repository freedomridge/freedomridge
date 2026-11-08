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


def test_production_compose_pulls_this_release_s_published_images():
    # docker-compose.prod.yml names each release's images (.github/workflows/images.yml publishes them), so the
    # tags must move with VERSION, and every image it names must be one the workflow builds.
    compose = (ROOT / "docker-compose.prod.yml").read_text()
    named = re.findall(r"^\s+image: ghcr\.io/freedomridge/([a-z-]+):(\S+)$", compose, re.M)
    assert len(named) == 6, named  # db, backend, worker, job-runner, backup, frontend
    assert {f"{image}:{tag}" for image, tag in named if tag != VERSION} == set()
    workflow = (ROOT / ".github/workflows/images.yml").read_text()
    built = re.search(r"^\s+image: \[([a-z, -]+)\]$", workflow, re.M)
    assert built, "images.yml has no image matrix"
    published = {name.strip() for name in built.group(1).split(",")}
    assert {image for image, _ in named} <= published
    for image in published:
        assert (ROOT / f"Dockerfile.{image}").is_file(), f"images.yml builds {image} from a missing Dockerfile.{image}"
