# Updating dependencies

Every version Freedom Ridge runs with is written down in the repository. Updates are proposed
every month in the maintainers' working repository, checked by CI, and arrive here with the next
release.

## Where versions live

| What | Range or choice | Exact versions |
|---|---|---|
| Python packages: server, SDK, importers, dev tools | `pyproject.toml`, `python/pyproject.toml` | `uv.lock` (with hashes) |
| PyTorch build | `cpu` / `cuda` extras in `pyproject.toml` | `uv.lock` |
| Frontend packages | `frontend/package.json` | `frontend/package-lock.json` |
| Python version | `.python-version`, `requires-python` | `python:3.14-slim@sha256:…` in `Dockerfile.backend` and `Dockerfile.worker`; `python-version` in `.github/workflows/ci.yml` |
| Node.js version | `.nvmrc` | `node:26-alpine@sha256:…` in `Dockerfile.frontend`; `node-version` in CI |
| Other Docker images | | `tag@sha256:…` in the Dockerfiles and `docker-compose*.yml` |
| GitHub Actions | | `uses:` lines in `.github/workflows/ci.yml` |

`uv sync` installs exactly what `uv.lock` says into `.venv`. Always pass one extra:
`--extra cuda` on a machine with an NVIDIA GPU (driver 560 or newer), `--extra cpu`
everywhere else. Without one, PyTorch would come from PyPI, whose CUDA build needs a newer driver.

## Monthly update pull request

In the maintainers' working repository, Dependabot opens one pull request a month that combines
the updates for every ecosystem (Python, npm, Docker images, Compose images, GitHub Actions),
major versions included.

To review it: wait for CI to pass. For a major update, read the release notes linked in the
pull request and run the affected feature locally. Then merge it. If one update needs code
changes, make them on the pull request's branch, or skip that update in
`.github/dependabot.yml` (Dependabot then rebuilds the pull request without it) and make it by
hand. Pull requests that change `.github/workflows/` can only be merged by someone whose GitHub
access includes the `workflow` permission (merging in the web UI works).

These moves are never proposed automatically, because they must be made by hand (below):
the Python minor version, Node.js and PostgreSQL major versions, and transformers 5.
Two frontend versions are skipped too:

- TypeScript 7 waits for typescript-eslint and the unit tests' `ts.transpileModule`: 7.0 ships
  without the compiler API they call, and TypeScript plans to restore it in 7.1. 7.1 fails lint
  until typescript-eslint accepts it. Drop the skip when typescript-eslint supports 7.1.
- ESLint 10 waits for eslint-plugin-react, eslint-plugin-import and eslint-plugin-jsx-a11y (all
  pulled in by eslint-config-next) to support it. Drop the skip when `npm ls` accepts ESLint 10.

## Upgrading by hand

Run the checks from [CONTRIBUTING.md](../CONTRIBUTING.md#checks) after each change, then
commit the changed `pyproject.toml`/`uv.lock` or `package.json`/`package-lock.json` together.

- **One Python package:** `uv lock --upgrade-package <name>`, then `uv sync --extra cpu`.
- **All Python packages:** `uv lock --upgrade`.
- **Add a Python package:** server: `uv add <name>`; dev tool: `uv add --group dev <name>`;
  SDK: add it to `python/pyproject.toml`, then run `uv lock`.
- **Frontend packages:** `cd frontend && npm install <name>@latest` (or `npm update`), then
  `npm audit --omit=dev`.
- **Python version** (e.g. 3.14 → 3.15): change `.python-version`; change the `python:` tag
  **and** digest in `Dockerfile.backend` and `Dockerfile.worker`; change every
  `python-version:` in `.github/workflows/ci.yml`; then `uv lock` and `uv sync --extra cpu`.
  uv downloads the new Python if it is missing.
- **Node.js version:** change `.nvmrc`, both `FROM node:` lines in `Dockerfile.frontend`, and
  every `node-version:` in CI; then `nvm install`, `nvm use` and `cd frontend && npm ci`.
- **Newer CUDA build of PyTorch:** the `cuda` extra uses PyTorch's CUDA 12.6 build, which
  runs on driver 560 or newer. PyTorch 2.14 is the last release built for CUDA 12.6; 2.15 and
  later ship only CUDA 13 builds, which need driver 580 or newer. PyTorch stays on 2.14 until
  then, because updating it alone would move only the `cpu` extra to 2.15. After upgrading the
  driver, change the `pytorch-cuda` index URL in `pyproject.toml` to
  `https://download.pytorch.org/whl/cu132`, then `uv lock --upgrade-package torch` and
  `uv sync --extra cuda`.
- **PostgreSQL major version:** dump and restore as described in
  [Backup and migration](BACKUP_AND_MIGRATION.md); the backup image's `pg_dump` must not
  be newer than the server it restores into.

## Known audit exceptions

CI's Python dependency audit (`pip-audit`, quality job) fails on any known vulnerability
except the advisories below. Each one was reviewed and judged not to apply to how
Freedom Ridge uses the package. Remove the `--ignore-vuln` flags in CI when the condition
in the last column is met.

| Package | Advisories | Why it is accepted | Remove when |
|---|---|---|---|
| transformers 4.x | PYSEC-2025-217, PYSEC-2026-2288, PYSEC-2026-2289, PYSEC-2026-2290, PYSEC-2026-3929, PYSEC-2026-4174 | Each needs a malicious model repository or config, the `Trainer`, a conversion script, `save_pretrained` on attacker input, or custom generation code. Freedom Ridge only loads the CLIP and DINOv3 checkpoints that an admin or writer registered. The fixes exist only in transformers 5, which changes image preprocessing: before upgrading, check that new query embeddings still match the stored ones. | transformers 5 is adopted |
| setuptools 81 | PYSEC-2026-3447, GHSA-xmj3-9gf7-h52w | PyTorch requires `setuptools<82`. The advisories concern building source distributions (`MANIFEST.in`) and legacy Windows egg installs, neither of which happens when running Freedom Ridge. | PyTorch allows setuptools 84.1 or newer |
