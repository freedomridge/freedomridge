"""The product version: the one line in the repository's VERSION file (copied into the images).

pyproject.toml, python/pyproject.toml and frontend/package.json carry the same number;
backend/tests/test_version.py fails when any of them differs."""

from pathlib import Path

VERSION = (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip()
