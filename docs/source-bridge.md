# Source bridge

The source bridge turns HTML listing pages into RSS feeds. It can also fetch articles that require a login or a browser session.

You do not need the bridge if your sites already provide useful RSS feeds.

## Start with Docker

Copy the example configuration and create an access token:

```bash
cp source-bridge.example.toml source-bridge.toml
export SOURCE_BRIDGE_ACCESS_TOKEN="$(openssl rand -hex 32)"
docker compose up --build -d source-bridge
```

The root Compose file keeps the bridge on the internal Docker network. A FreshRSS container on that network can subscribe to:

```text
http://source-bridge:8100/synthetic/{source_id}.xml
```

Set the feed username to `source-bridge`. Set the feed password to `SOURCE_BRIDGE_ACCESS_TOKEN`.

If RSS Kindle uses the bridge to extract articles, give both services the same token and set:

```dotenv
SOURCE_BRIDGE_API_URL=http://source-bridge:8100
```

## Configure a source

The bridge reads TOML from `SOURCE_BRIDGE_CONFIG_PATH`. This example creates a feed named `example-home`:

```toml
[sources.example-home]
title = "Example News"
start_urls = ["https://example.com/news"]
link_selector = "main a[href]"
include_url_patterns = [
  "^https://example\\.com/articles/",
]
max_items = 20
refresh_seconds = 900
```

FreshRSS can subscribe to:

```text
http://source-bridge:8100/synthetic/example-home.xml
```

Each source supports these main settings:

| Setting | Purpose |
| --- | --- |
| `title` | Feed title |
| `start_urls` | Listing pages to scan |
| `link_selector` | CSS selector for links on each listing page |
| `include_url_patterns` | Regular expressions for URLs to include |
| `exclude_url_patterns` | Regular expressions for URLs to exclude |
| `auth_profile` | Optional authentication profile name |
| `fetch_backend` | `http` or `browser` |
| `max_items` | Maximum number of items in the feed |
| `refresh_seconds` | Time before the cached feed becomes stale |

See [`source-bridge.example.toml`](../source-bridge.example.toml) for a browser-backed example. Keep your real configuration out of git because it can contain private sources and local paths.

## Choose a fetch method

Start with `fetch_backend = "http"`. It is the fastest option and works for public pages that return complete HTML.

Use `fetch_backend = "browser"` when a site requires login or renders its content with JavaScript. A browser source must refer to an authentication profile with either `browser_profile_path` or `browser_cdp_url`.

```toml
[auth_profiles.example]
domains = ["example.com"]
browser_profile_path = "./data/browser-profiles/example"
browser_headless = true

[sources.example-home]
title = "Example News"
start_urls = ["https://example.com/news"]
link_selector = "main a[href]"
include_url_patterns = ["^https://example\\.com/articles/"]
auth_profile = "example"
fetch_backend = "browser"
browser_wait_for_selector = "article, main"
max_items = 20
```

Authentication profiles support:

| Setting | Purpose |
| --- | --- |
| `domains` | Domains that can use the profile |
| `headers` | Extra request headers |
| `cookie_header` | Cookie header stored in the TOML file |
| `cookie_jar_path` | Netscape cookie file or a file that contains a Cookie header |
| `browser_profile_path` | Persistent browser profile directory |
| `browser_cdp_url` | Address of an existing Chromium browser |
| `browser_executable_path` | Explicit browser executable |
| `browser_channel` | Installed browser channel, such as `chrome` |
| `browser_headless` | Run the launched browser without a visible window |
| `browser_launch_args` | Extra browser arguments |

Browser sources can set separate load conditions for listing and article pages:

- `browser_wait_until`, `browser_wait_for_selector`, and `browser_settle_seconds`
- `discovery_browser_wait_until`, `discovery_browser_wait_for_selector`, and `discovery_browser_settle_seconds`

Use a dedicated browser profile for each account. Do not reuse your daily browser profile. For a host-native browser, set `browser_channel = "chrome"` or `browser_executable_path`. The Docker image already contains Chromium.

## Authentication

The bridge requires `SOURCE_BRIDGE_ACCESS_TOKEN` at startup. Every endpoint except `/health` checks this token.

Clients can send it with:

- HTTP Basic authentication with username `source-bridge` and the token as the password
- `Authorization: Bearer <token>`
- `X-Source-Bridge-Token: <token>`

FreshRSS should use HTTP Basic authentication. Do not add the token to a feed URL because URLs can appear in logs and browser history.

## Refresh behavior

The bridge stores feed state in SQLite. When FreshRSS requests a feed, the bridge:

1. Returns the cached feed if it is current.
2. Starts a background refresh if the cached feed is stale.
3. Waits for a refresh only when the feed has no cached items.
4. Keeps the last good feed if a later refresh fails.

Background prewarming is on by default. It checks for stale sources before FreshRSS asks for them.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Container health check; no token required |
| `GET /sources` | List configured sources |
| `GET /status` | Show item counts and refresh status |
| `POST /sources/{source_id}/refresh` | Schedule a refresh |
| `GET /synthetic/{source_id}.xml` | Return a synthetic RSS feed |
| `GET /extract?url=...` | Extract one article for RSS Kindle |

Treat `/extract` as a private helper endpoint, not as a public article proxy.

## Run locally

```bash
cp source-bridge.example.toml source-bridge.toml
uv sync --extra dev
DATABASE_PATH=data/source_bridge.db \
  SOURCE_BRIDGE_CONFIG_PATH=source-bridge.toml \
  SOURCE_BRIDGE_ACCESS_TOKEN=dev-only-token \
  uv run uvicorn app.source_main:create_app --factory --reload --port 8100
```

## Environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `DATABASE_PATH` | SQLite database for feed state | `data/rss_kindle.db` outside Compose |
| `SOURCE_BRIDGE_CONFIG_PATH` | TOML configuration path | unset |
| `SOURCE_BRIDGE_ACCESS_TOKEN` | Token for protected endpoints | none; required at startup |
| `SOURCE_BRIDGE_REFRESH_SECONDS` | Default cache lifetime | `900` |
| `SOURCE_BRIDGE_PREWARM_ENABLED` | Refresh sources in the background | `true` |
| `SOURCE_BRIDGE_PREWARM_INTERVAL_SECONDS` | Time between prewarm checks | `60` |
| `APP_ALLOWED_HOSTS` | Comma-separated allowed hostnames | unset |
| `HTTP_TIMEOUT_SECONDS` | Outbound HTTP timeout | `20` |
| `USER_AGENT` | User agent for HTTP requests | project default |

## Security

- Keep the bridge on a private Docker network.
- Use a long, random access token.
- Keep TOML files, cookies, and browser profiles out of git and container images.
- Store browser profiles under the ignored `data/` directory.
- Automate only sites and accounts that you have permission to access.
