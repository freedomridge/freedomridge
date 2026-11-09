# freedomridge

Run [Freedom Ridge](https://github.com/freedomridge/freedomridge), the self-hosted tool for exploring and
curating computer vision datasets, on this machine:

```bash
npx freedomridge
```

You need Docker (Docker Desktop on macOS, Docker Engine with Compose v2 on Linux; on Windows, run it inside
WSL 2) and Node.js 18 or newer. It runs on x86_64 and Arm machines, Apple silicon included.

It puts this release's Compose file in `~/freedomridge`, pulls the release's images from ghcr.io (a few GB the
first time), starts them and opens http://localhost:3000. Log in as `admin` / `admin123` the first time, and
choose a new password. Put your images in `~/freedomridge/data`: the app sees them at the same paths.

The Python SDK connects to the API on port 8000:

```bash
pip install freedomridge
```

```python
import fridge
fridge.connect("http://localhost:8000/api", api_token="...")  # a token from Settings -> API Tokens
```

## Commands

| Command | |
|---|---|
| `npx freedomridge` | Start it. A folder running an earlier version is upgraded: its database is migrated first. |
| `npx freedomridge stop` | Stop it. The data stays. |
| `npx freedomridge status` | What runs, and where. |
| `npx freedomridge logs [service]` | Follow the logs: all, or one of `db`, `backend`, `job-runner`, `frontend`. |

Options: `--dir DIR` (another folder), `--port N` and `--api-port N` (other ports, kept for later starts),
`--no-open`, `--no-pull`. Set `MAPBOX_TOKEN` in the shell, or in the folder's `.env`, to turn the map on.

`npx freedomridge@X.Y.Z` runs that release. The launcher refuses to start a folder that a newer release runs.

It's for one machine: the credentials are the development ones, and the ports listen on 127.0.0.1 only. A
server needs `docker-compose.prod.yml` ([Docker deployment](https://github.com/freedomridge/freedomridge/blob/main/docs/DOCKER_DEPLOYMENT.md)).

To delete everything, data included: `docker compose -f ~/freedomridge/docker-compose.try.yml down -v`.
