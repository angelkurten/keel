# Hosted Keel: the control plane

The service behind hosted Keel (`docs/design/hosted.md`). It is not part of the `keel-context` package.

- **Sign-up**, by invitation code during the pilots: account slug, email, and a cartridge (uploaded, or one of the examples).
- **One database per account**: a MySQL database and a user with grants on it alone.
- **One Keel container per account** on Coolify: the official image, the cartridge mounted at `/cartridge`, `KEEL_DB` pointing at the account's database, `KEEL_ACCOUNT` set, a subdomain under `HOSTED_BASE_DOMAIN`.
- **The usage panel**: the account's key in, its events and tool calls per month out.

The account's container holds no credential but its own database's: a cartridge carries Python, which must not reach other accounts or the control plane. The control plane never imports a customer's `handlers.py`; it checks the YAML only. The key's record (hash only) and the account's limits are written to the account's database, where its container reads them; usage is read back from there.

Known limit: a customer's handlers can write to their own database, so they could alter their own usage counters or limits. Fine for invited pilots; before billing strangers, meter at the edge or with a separate database user.

## Configuration

| Variable | |
|---|---|
| `HOSTED_CONTROL_DB` | the control database (`mysql://...`): accounts, master key copies, the registry of hosted accounts |
| `HOSTED_MYSQL_ADMIN_URL` | a MySQL user that may create databases and users (`mysql://root:...@mysql:3306/`) |
| `HOSTED_MYSQL_HOST_FOR_ACCOUNTS` | the MySQL hostname as account containers reach it, when it differs from the admin URL's |
| `COOLIFY_URL`, `COOLIFY_TOKEN` | the Coolify instance and an API token that may create and deploy applications |
| `COOLIFY_PROJECT_UUID`, `COOLIFY_SERVER_UUID` | where account containers go |
| `HOSTED_BASE_DOMAIN` | accounts get `https://<account>.<base domain>`; point a wildcard DNS record at the Coolify server |
| `KEEL_IMAGE_TAG` | the Keel image version account containers run, e.g. `0.27.1` |
| `HOSTED_INVITE_CODES` | comma-separated codes that may sign up; empty closes sign-up |
| `HOSTED_DEFAULT_RPS`, `HOSTED_DEFAULT_EVENTS_MONTH` | limits for a new account (10 and 100000) |

## Run

```bash
docker build -f deploy/hosted/Dockerfile -t keel-hosted .
docker run -p 8080:8080 --env-file hosted.env keel-hosted
```

On Coolify: a MySQL database, this image (or a Dockerfile application from this repo with `deploy/hosted/Dockerfile`) on port 8080 with the variables above, and a wildcard DNS record for the account subdomains. Retention and the semantic index run per account container as scheduled tasks (`keel forget --policy`, `keel index`).
