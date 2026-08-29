# Deployment

Keep environment-specific deployment overlays out of git.

Use the committed root `docker-compose.yml` and the examples under `examples/`
as the portable base.

Keep real deployment files such as:

- host-specific Compose overlays
- reverse-proxy configs with real domains
- real `.env` files
- real `source-bridge.toml` files
- cookie/header files
- browser-profile directories
- LAN IPs or machine inventory notes
- local operator notes such as `AGENTS.local.md`

in a separate private ops repo or in a gitignored directory such as:

- `deploy/private/`
- `deploy/local/`

## Host-Neutral Deployment And Backups

Run the Docker Compose stack on a host, VM, or container platform that supports persistent volumes. Do not run the application as root. The shipped Compose files use a read-only root filesystem, remove Linux capabilities, and set a numeric user and group.

Prepare the persistent directories so that this user can write to them. The default is UID and GID `1000`. Set `RSS_KINDLE_UID` and `RSS_KINDLE_GID` if the host uses different values.

Use two backup layers on any host:

1. Configure your host or storage provider to copy all persistent volumes and private config to separate storage on a schedule. Use a consistent volume snapshot or stop the services during the copy.
2. Create RSS Kindle application archives from `/dashboard` or with `docker compose exec rss-kindle python -m app.backup_cli`. Copy important archives to storage outside the deployment.

Do not treat the application archive as a complete deployment backup. It excludes FreshRSS data, `.env` secrets, cookies, and browser profiles. This design keeps the archive portable across a local server, a VM, and a cloud host.
