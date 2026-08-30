# RSS Kindle

A small, fast FreshRSS frontend built for Kindle browsers.

FreshRSS still handles subscriptions, polling, unread state, and stars. RSS Kindle provides a simpler reading screen, extracts cleaner article text, and caches it locally. It is designed for one FreshRSS account rather than as a public, multi-user service.

<p align="center">
  <img src="docs/images/kindle-unread-queue.jpg" alt="Unread queue on a Kindle" width="280" />
  <img src="docs/images/kindle-article-view.jpg" alt="Article view on a Kindle" width="280" />
</p>

## Highlights

- Three-card pages and large side controls instead of unreliable Kindle scrolling
- Full-article extraction with a local SQLite cache
- FreshRSS groups, feeds, unread state, and stars
- Articles stay unread until you move past their final page
- Optional password login and long-lived Kindle pairing codes
- A small dashboard for health, devices, bridge refreshes, and backups
- An optional source bridge for sites with missing, incomplete, or protected feeds

## Requirements

- Docker, or Python 3.11+ with [uv](https://docs.astral.sh/uv/)
- A FreshRSS account with API access enabled
- The API password for that FreshRSS account

RSS Kindle accepts either a FreshRSS base URL or the full Google Reader API URL. See the FreshRSS documentation for [mobile access](https://freshrss.github.io/FreshRSS/en/users/06_Mobile_access.html) and the [Google Reader API](https://freshrss.github.io/FreshRSS/en/developers/06_GoogleReader_API.html).

## Quick start

This is the usual setup when FreshRSS already runs somewhere else.

```bash
cp .env.example .env
```

Set these values in `.env`:

```dotenv
FRESHRSS_API_URL=https://freshrss.example.com
FRESHRSS_USERNAME=reader
FRESHRSS_API_PASSWORD=replace-me
```

Then start the reader:

```bash
docker compose up --build -d rss-kindle
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

The container runs as UID and GID `1000` by default. If that user cannot write to `./data`, set `RSS_KINDLE_UID` and `RSS_KINDLE_GID` to the owner of the directory.

### Protect the reader

Add all three values to `.env`:

```dotenv
APP_AUTH_USERNAME=reader
APP_AUTH_PASSWORD=replace-me
APP_AUTH_SECRET=replace-with-a-long-random-string
```

Keep `APP_SECURE_COOKIES=true` behind HTTPS. Set it to `false` only for plain HTTP on a trusted local network.

## Deployment options

### Reader and FreshRSS

The [reader-with-freshrss example](examples/reader-with-freshrss) runs both services on one Docker network.

Review the volume paths in the example before you use it on a server.

Start FreshRSS first:

```bash
docker compose -f examples/reader-with-freshrss/docker-compose.yml up -d freshrss
```

Complete FreshRSS setup at [http://127.0.0.1:8081](http://127.0.0.1:8081), enable API access for the reader account, and then start RSS Kindle:

```bash
FRESHRSS_USERNAME=reader \
FRESHRSS_API_PASSWORD=replace-me \
docker compose -f examples/reader-with-freshrss/docker-compose.yml up --build -d rss-kindle
```

### Full stack

The [full-stack example](examples/full-stack) adds Caddy and `source-bridge`. Use it only when you need synthetic feeds or authenticated extraction. Change the hostnames in [`examples/full-stack/Caddyfile`](examples/full-stack/Caddyfile) before you start it, and make sure they resolve to the Docker host.

```bash
cp source-bridge.example.toml source-bridge.toml
export RSS_KINDLE_SOURCE_BRIDGE_CONFIG="$(pwd)/source-bridge.toml"
export SOURCE_BRIDGE_ACCESS_TOKEN="$(openssl rand -hex 32)"

FRESHRSS_USERNAME=reader \
FRESHRSS_API_PASSWORD=replace-me \
docker compose -f examples/full-stack/docker-compose.yml up --build -d
```

For a new FreshRSS install, create the `reader` account and give it the same API password after the stack starts. See [Source bridge](docs/source-bridge.md) for configuration, authentication, browser profiles, and synthetic feed URLs.

## Using the reader

The list shows three articles at a time. Use the left and right edges to move between pages. The header shows the current page and article range; its **Menu** contains categories, starred items, feeds, the read filter, Dashboard, and Log out.

Opening an article does not mark it read. The side controls move one screen at a time. Moving right after the final screen marks the article read and opens the next one. Home opens **All articles**; × returns to the list and card you came from.

The footer contains Back to list, Star or Unstar, Mark read or unread, and Open original.

## Kindle pairing

Password authentication is optional, but it is useful if the reader is reachable outside a trusted network. Password sessions can open the dashboard. Paired Kindle sessions can only use the reader.

To pair a Kindle:

1. Sign in on a laptop or phone.
2. Open `/dashboard` and select **New pairing code**.
3. Open `/activate` on the Kindle.
4. Enter the six-digit code and an optional device name.

The code lasts 10 minutes by default. A paired device remains signed in for one year and can be revoked from the dashboard.

## Configuration

The environment template is [`.env.example`](.env.example). These are the settings most deployments need:

| Variable | Purpose | Default |
| --- | --- | --- |
| `FRESHRSS_API_URL` | FreshRSS base or Google Reader API URL | required |
| `FRESHRSS_USERNAME` | FreshRSS account name | required |
| `FRESHRSS_API_PASSWORD` | API password for that account | required |
| `DATABASE_PATH` | Reader cache and device database | `data/rss_kindle.db` |
| `APP_AUTH_USERNAME` | Optional reader login name | unset |
| `APP_AUTH_PASSWORD` | Optional reader login password | unset |
| `APP_AUTH_SECRET` | Session-signing secret; required with app auth | unset |
| `APP_SECURE_COOKIES` | Send cookies over HTTPS only | `true` |
| `APP_ALLOWED_HOSTS` | Comma-separated allowed hostnames | unset |
| `DEVICE_SESSION_MAX_AGE_SECONDS` | Paired-device lifetime | one year |
| `BACKUP_DIRECTORY` | Application backup directory | `data/backups` |
| `BACKUP_RETENTION_COUNT` | Application backups to retain | `7` |
| `SOURCE_BRIDGE_API_URL` | Source bridge URL; used only with a bridge token | `http://source-bridge:8100` in bridge Compose files |
| `SOURCE_BRIDGE_ACCESS_TOKEN` | Shared bridge token | required when the bridge runs |

## Dashboard and backups

The password-only dashboard shows FreshRSS status, cache statistics, paired devices, source bridge status, and retained backups.

Create an application backup from the dashboard or the command line:

```bash
docker compose exec rss-kindle python -m app.backup_cli
```

These ZIP files contain the RSS Kindle database and, when present, the source bridge database. They do not include FreshRSS data, `.env`, cookies, browser profiles, or private configuration. Back up the persistent volumes and private configuration separately. See [Deployment](deploy/README.md) for the recommended split between application archives and host backups.

## Security

- Enable the built-in login if the reader is not confined to a trusted network.
- Use HTTPS and leave secure cookies enabled on public deployments.
- Keep `.env`, `source-bridge.toml`, cookies, and browser profiles out of git.
- Set `SOURCE_BRIDGE_ACCESS_TOKEN` whenever `source-bridge` runs.
- Restrict `APP_ALLOWED_HOSTS` to the hostnames you use.
- The supplied Compose files run the app without root, remove Linux capabilities, and use a read-only root filesystem.

## Development

```bash
uv sync --extra dev
export FRESHRSS_API_URL=https://freshrss.example.com
export FRESHRSS_USERNAME=reader
export FRESHRSS_API_PASSWORD=replace-me
uv run uvicorn app.main:create_app --factory --reload
```

Run the tests:

```bash
uv run pytest -q
```

Run a repeatable server benchmark against a reader instance:

```bash
uv run rss-kindle-benchmark https://reader.example.com --path / --requests 20
```

Desktop benchmarks help catch regressions, but they do not reproduce e-ink refresh time. Check interaction and page turns on a physical Kindle before a release.

## License

[MIT](LICENSE)
