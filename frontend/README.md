# Freedom Ridge frontend

The browser app: Next.js (App Router), React and TypeScript. The browser always calls the
relative `/api` path; the Next.js server forwards it to the backend. See the
[Quick Start](../docs/QUICK_START.md) for the whole stack.

## Development

Use Node.js 26 (`nvm use` reads the repository's `.nvmrc`) and start the backend first.

```bash
npm ci
cp .env.local.example .env.local
npm run dev
```

The app runs at http://localhost:3000.

## Environment variables

| Variable | Read | Purpose |
|---|---|---|
| `MAPBOX_TOKEN` | when the server runs (`/runtime-config`) | Mapbox public token for the map panel. Everything else works without it. Use a public token with URL restrictions |
| `NEXT_PUBLIC_MAPBOX_TOKEN` | at build time (compiled in) | The map's token when `MAPBOX_TOKEN` isn't set: in `.env.local` for development |
| `FRIDGE_LIVE_ADDRESS` | when the server runs (`/runtime-config`) | `off` (or `false`, `0`, `no`) keeps filters, the sort and the open sample out of the dataset and snapshot pages' address ([links](../docs/DATASET_MANAGEMENT.md#switching-the-live-address-off)). On when unset |
| `FRIDGE_BACKEND_URL` | when `next.config.ts` loads (dev server start or build) | Where `/api` is forwarded; default `http://localhost:8000`. The Docker image bakes it in at build time |
| `ALLOWED_DEV_ORIGINS` | dev server only | Comma-separated hosts or IPs allowed to load `next dev` from another machine |
| `NEXT_DIST_DIR` | dev server and build | Output directory instead of `.next`, so a second dev server can run from the same checkout. Next.js then adds that directory to `tsconfig.json`; do not commit that change |

Put them in `.env.local` ([`.env.local.example`](.env.local.example) has comments). Restart the
dev server after changing them. The backend must also list the frontend's origin in
`ALLOWED_ORIGINS`; see [authentication](../docs/AUTHENTICATION.md#csrf-and-allowed-origins).

## Build and checks

```bash
npm run build                 # production build (standalone output, used by Dockerfile.frontend)
npx tsc --noEmit
npm run lint
node --test tests/*.test.cjs  # unit tests
```

`npm run test:e2e` runs the Playwright suite. Do not run it with its defaults: it targets
http://localhost:3000 and http://localhost:8000/api and deletes `e2e-smoke` datasets there.
[CONTRIBUTING.md](../CONTRIBUTING.md#7-browser-tests-ci-e2e) shows how to point it at a disposable
stack.

## Code

- `app/`: routes (datasets, dataset view, settings, users, login).
- `components/`: panels, modals and the filter sidebar.
- `contexts/`: shared state, including filters and the current user.
- `lib/`: the API client and helpers.

[docs/panels.md](../docs/panels.md) explains how panels and filters fit together, and
[docs/MAP_REQUIREMENTS.md](../docs/MAP_REQUIREMENTS.md) is the contract for the map panel.
