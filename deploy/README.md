# Deployment notes

The Compose files in this repository are portable examples. Keep settings for a real host in a private repository or an ignored directory such as `deploy/private/`.

Do not commit:

- `.env` files
- host-specific Compose files
- real domains, LAN addresses, or machine notes
- `source-bridge.toml`
- cookies or exported request headers
- browser profiles

## Container permissions

RSS Kindle runs with a read-only root filesystem and no Linux capabilities. The app uses UID and GID `1000` by default.

Make sure this user can write to each persistent directory. Set `RSS_KINDLE_UID` and `RSS_KINDLE_GID` if your host uses a different owner.

## Backups

Use both of these backup methods:

1. Back up the persistent volumes and private configuration to another machine or storage service. Use a consistent volume snapshot or stop the services during the copy.
2. Create an RSS Kindle archive from the dashboard or run:

   ```bash
   docker compose exec rss-kindle python -m app.backup_cli
   ```

The application archive contains the RSS Kindle database and, when available, the source bridge database. It does not contain FreshRSS data, environment variables, cookies, browser profiles, or private configuration.

Copy important application archives away from the deployment host. Do not use them as your only backup.
