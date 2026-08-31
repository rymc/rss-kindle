# RSS Kindle

RSS Kindle is a fast, simple FreshRSS reader built for Kindle browsers and e-ink screens.

## Main features

- Paged article lists and local page turns
- Clean article text without images or media
- FreshRSS feeds, groups, unread state, and starred articles
- Articles remain unread until you leave their final page
- Optional login, Kindle pairing, and an operations dashboard
- An optional source bridge for feeds that need browser access

## How it works on Kindle

RSS Kindle limits network requests and browser work. Each article-list response contains 15 articles, shown as five pages of three cards. Only the first page requires a server request. Article page turns are also local. The Kindle contacts the server when it opens another article.

RSS Kindle also:

- compresses HTML, CSS, and JavaScript and caches static files
- caches FreshRSS responses
- removes article images and media
- does not use animations, smooth scrolling, or web fonts
- avoids third-party browser requests
- updates only the content needed for each page turn

### Browser benchmark

This controlled browser test compares FreshRSS 1.29.1 with RSS Kindle. Chromium ran at 600 × 800 with 6× CPU slowdown, 150 ms added latency, and a 1 Mbit/s connection. Each result is the median of 10 loads after one warmup load.

| Metric | FreshRSS | RSS Kindle | Difference |
| --- | ---: | ---: | ---: |
| Cold transferred data | 117.3 KB | 10.8 KB | 91% less |
| Warm transferred data | 6.6 KB | 0.3 KB | 95% less |
| Cold resource requests | 22 | 3 | 86% fewer |
| Cold first contentful paint | 580 ms | 418 ms | 162 ms lower |

Warm loads use cached files. Cold loads use an empty browser cache. These browser results do not include the physical e-ink refresh.

## Requirements

- Docker, or Python 3.11+ with [uv](https://docs.astral.sh/uv/)
- A FreshRSS account with API access enabled
- The API password for that FreshRSS account

RSS Kindle accepts a FreshRSS base URL or a full Google Reader API URL. See the FreshRSS documentation for [mobile access](https://freshrss.github.io/FreshRSS/en/users/06_Mobile_access.html) and the [Google Reader API](https://freshrss.github.io/FreshRSS/en/developers/06_GoogleReader_API.html).

## Quick start

Use these steps when FreshRSS runs on another server.

Copy the environment template:

```bash
cp .env.example .env
```

Set these values in `.env`:

```dotenv
FRESHRSS_API_URL=https://freshrss.example.com
FRESHRSS_USERNAME=reader
FRESHRSS_API_PASSWORD=replace-me
```

Start RSS Kindle:

```bash
docker compose up --build -d rss-kindle
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

The container uses UID and GID `1000` by default. If this user cannot write to `./data`, set `RSS_KINDLE_UID` and `RSS_KINDLE_GID` to the directory owner's values.

### Optional login

Enable the login if users can reach RSS Kindle outside your trusted network. Add these values to `.env`:

```dotenv
APP_AUTH_USERNAME=reader
APP_AUTH_PASSWORD=replace-me
APP_AUTH_SECRET=replace-with-a-long-random-string
```

Keep `APP_SECURE_COOKIES=true` when you use HTTPS. Set it to `false` only for plain HTTP on a trusted local network.

## Using the reader

The article list shows three articles at a time. Use the left and right edges to change pages. The header shows the page number and article range. Open **Menu** for categories, starred articles, feeds, the read filter, Dashboard, and Log out.

Opening an article does not mark it as read. The side controls move one screen at a time. Move right from the final screen to mark the article as read and open the next article. Home opens **All articles**. × returns to the article list and the card that you opened.

The footer contains Back to list, Star or Unstar, Mark read or unread, and Open original.

## Deployment options

### RSS Kindle with FreshRSS

The [reader-with-freshrss example](examples/reader-with-freshrss) runs both services on one Docker network.

Set the correct volume paths before you start the services. Start FreshRSS first:

```bash
docker compose -f examples/reader-with-freshrss/docker-compose.yml up -d freshrss
```

Complete the FreshRSS setup at [http://127.0.0.1:8081](http://127.0.0.1:8081). Enable API access for the reader account. Then start RSS Kindle:

```bash
FRESHRSS_USERNAME=reader \
FRESHRSS_API_PASSWORD=replace-me \
docker compose -f examples/reader-with-freshrss/docker-compose.yml up --build -d rss-kindle
```

### Full stack

The [full-stack example](examples/full-stack) adds Caddy and `source-bridge`. Use it when you need synthetic feeds or article extraction with authentication.

Change the hostnames in [`examples/full-stack/Caddyfile`](examples/full-stack/Caddyfile). Make sure that they resolve to the Docker host. Then start the stack:

```bash
cp source-bridge.example.toml source-bridge.toml
export RSS_KINDLE_SOURCE_BRIDGE_CONFIG="$(pwd)/source-bridge.toml"
export SOURCE_BRIDGE_ACCESS_TOKEN="$(openssl rand -hex 32)"

FRESHRSS_USERNAME=reader \
FRESHRSS_API_PASSWORD=replace-me \
docker compose -f examples/full-stack/docker-compose.yml up --build -d
```

For a new FreshRSS installation, create the `reader` account after the stack starts. Give it the same API password. See [Source bridge](docs/source-bridge.md) for configuration, authentication, browser profiles, and synthetic feed URLs.

## Kindle pairing

Password sessions can use the reader and dashboard. Paired Kindle sessions can use only the reader.

To pair a Kindle:

1. Sign in on a laptop or phone.
2. Open `/dashboard` and select **New pairing code**.
3. Open `/activate` on the Kindle.
4. Enter the six-digit code and an optional device name.

The code is valid for 10 minutes by default. A paired device remains signed in for one year. You can revoke its access from the dashboard.

## Configuration

The environment template is [`.env.example`](.env.example). Most deployments use these settings:

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

## Operations and security

The password-only dashboard shows FreshRSS status, cache statistics, paired devices, source bridge status, and retained backups.

Create an application backup from the dashboard or run:

```bash
docker compose exec rss-kindle python -m app.backup_cli
```

Each ZIP file contains the RSS Kindle database and, when present, the source bridge database. It does not contain FreshRSS data, `.env`, cookies, browser profiles, or private configuration. Back up the persistent volumes and private configuration separately. See [Deployment](deploy/README.md) for the recommended backup design.

For a secure deployment:

- Enable the login outside a trusted network.
- Use HTTPS and keep secure cookies enabled on public deployments.
- Keep `.env`, `source-bridge.toml`, cookies, and browser profiles out of git.
- Set `SOURCE_BRIDGE_ACCESS_TOKEN` when `source-bridge` runs.
- Set `APP_ALLOWED_HOSTS` to the hostnames that you use.
- Use the supplied Compose files to run the app without root, remove Linux capabilities, and use a read-only root filesystem.

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

Run a repeatable server benchmark against an RSS Kindle instance:

```bash
uv run rss-kindle-benchmark https://reader.example.com --path / --requests 20
```

Run the throttled browser benchmark:

```bash
uv run rss-kindle-browser-benchmark https://reader.example.com --path / --requests 10 --cold
```

The browser benchmark reports load time, transfer size, DOM size, layout shift, long tasks, and page-turn work. It helps find regressions but does not reproduce e-ink refresh time. Test interaction and page turns on a physical Kindle before a release.

## License

[MIT](LICENSE)
