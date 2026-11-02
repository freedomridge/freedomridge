# Security policy

Security fixes target the latest code on the default branch. Older snapshots do
not have a separate security maintenance commitment.

Please report suspected vulnerabilities privately through this repository's
**Security → Advisories → Report a vulnerability** form on GitHub. Include the
affected version or commit, reproduction steps, impact, and a minimal example.
Remove credentials, personal information, and private datasets from the report.

If private vulnerability reporting is unavailable, contact the maintainer through
the contact options on the [Freedom Ridge GitHub organization](https://github.com/freedomridge).
If no private channel is listed, open an issue asking for a private security
contact, without describing the vulnerability. Do not disclose exploit details
in a public issue or pull request before a fix is available.

The maintainer will coordinate investigation, a fix, and disclosure with you.

For production configuration, see [Deploy to AWS](docs/DEPLOY_TO_AWS.md) or
[Docker deployment](docs/DOCKER_DEPLOYMENT.md), and [authentication](docs/AUTHENTICATION.md)
for accounts, tokens and guest limits. Production MUST set:

- `FRIDGE_ENV=production`;
- a unique, randomly generated `STORAGE_ENCRYPTION_KEY`;
- `FRIDGE_ADMIN_PASSWORD`, which replaces the seeded `admin123` password on first start;
- `ALLOWED_ORIGINS` to the exact HTTPS origin users load the app from.

Keep `SESSION_COOKIE_SECURE` at its default, `true`, and serve the app over TLS.
With `FRIDGE_ENV=production` the backend refuses to start without a non-default
`STORAGE_ENCRYPTION_KEY` and, while the admin still has the seeded password, a valid
`FRIDGE_ADMIN_PASSWORD`. `docker-compose.prod.yml` also refuses to start without
`ALLOWED_ORIGINS`.
