# Port reference

Use [Deploy to AWS](DEPLOY_TO_AWS.md) or [Docker deployment](DOCKER_DEPLOYMENT.md)
for the certificate and environment setup before starting production.

| Mode | Frontend | API | Published ports |
|---|---|---|---|
| Production | `https://YOUR_DOMAIN` | `https://YOUR_DOMAIN/api` | nginx 443; 80 for ACME/redirect |
| Local development | `http://localhost:3000` | `http://localhost:8000/api` | PostgreSQL 5432 from `docker-compose.yml`, on all interfaces unless `FRIDGE_DB_PORT=127.0.0.1:5432`; 8000 and 3000 belong to the backend and frontend processes you start |

Production backend, frontend and PostgreSQL have no host port mappings. Bare IPs
and unknown Hosts are rejected; use the configured DNS hostname. Local sample
media uses authenticated `/api/samples/ID/media`; S3 media also passes through the
backend for authorization. nginx does not serve an unrestricted `/media` directory.

```python
from fridge import connect
connect("https://YOUR_DOMAIN/api")  # production
# connect("http://localhost:8000/api")  # local development
```

Remote ingestion also uses HTTPS and an API token. Host media paths must exist at
the same absolute paths inside the backend; see the deploy guide's mount contract.
For an isolated local TLS rehearsal, use loopback high ports such as 18080/18443,
a separate Compose project and disposable data, as shown in the Docker guide.
