# AWS operations

Follow [Deploy to AWS](../docs/DEPLOY_TO_AWS.md) for the supported EC2/Terraform
workflow: operator-only SSH, encrypted data/cache disks, generated secrets, DNS,
webroot TLS bootstrap/renewal, on-box demo imports, backups, alarms and teardown.
`smoke/guest_gate.sh` is the guest-permission release check from that guide.

`terraform/user_data.sh` deliberately prepares the host without starting the app.
Complete `.env` and certificate setup before starting `docker-compose.prod.yml`.
For local development, see [the quick start](../docs/QUICK_START.md).
