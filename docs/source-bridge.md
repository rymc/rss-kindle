# Source Bridge

`source-bridge` is an optional service that turns ordinary HTML pages into synthetic RSS feeds for FreshRSS.

It exists for the cases where FreshRSS alone is not enough:

- a site does not publish RSS
- a site's feed is incomplete or low quality
- a site requires login or a browser-backed session before article content is readable

If you only want the Kindle UI, you do not need this service.

## How It Works

At a high level, the bridge:

1. loads one or more configured listing pages
2. discovers article links from those pages
3. fetches article pages with plain HTTP or a browser-backed session
4. extracts readable article HTML
5. stores synthetic feed state in SQLite
6. serves a synthetic RSS feed that FreshRSS can subscribe to

`rss-kindle` can also use the bridge as an authenticated extraction helper for sites that FreshRSS can ingest but `rss-kindle` cannot fetch anonymously later.

## Endpoints

- `/health`: simple health check
- `/sources`: list configured source IDs
- `/status`: source item counts, refresh state, and the last attempt, success, or error
- `POST /sources/{source_id}/refresh`: start a manual background refresh; returns `202`
- `/synthetic/{source_id}.xml`: synthetic RSS feed endpoint for FreshRSS
- `/extract`: private helper endpoint used by `rss-kindle` for authenticated extraction fallback

When `SOURCE_BRIDGE_ACCESS_TOKEN` is set, all endpoints except `/health` require either:

- `X-Source-Bridge-Token: <token>`
- `Authorization: Bearer <token>`
- `?access_token=<token>`

Treat `/extract` as a private helper endpoint, not a public article proxy.

## Runtime Model

When FreshRSS requests `/synthetic/{source_id}.xml`, the bridge:

1. checks whether cached feed data is still fresh
2. serves cached content immediately when it can
3. refreshes in the background when content is stale but a cached feed already exists
4. refreshes synchronously when no cached feed exists yet
5. keeps serving the last good version if a later refresh fails

When `SOURCE_BRIDGE_PREWARM_ENABLED=true`, the bridge also runs a background loop that refreshes sources before they go stale. That reduces the chance that FreshRSS polls during a stale-while-refresh window.

## Quick Start

### Docker Compose

From the repo root:

```bash
cp source-bridge.example.toml source-bridge.toml
docker compose up --build -d source-bridge
```

FreshRSS can then subscribe to:

```text
http://<host>:8100/synthetic/{source_id}.xml
```

### Local Development

```bash
cp source-bridge.example.toml source-bridge.toml
uv sync --extra dev
uv run uvicorn app.source_main:create_app --factory --reload --port 8100
```

## Configuration File

The bridge reads a TOML file from `SOURCE_BRIDGE_CONFIG_PATH`.

The two main blocks are:

- `[auth_profiles.<name>]`: reusable auth and browser settings for one or more domains
- `[sources.<id>]`: one synthetic feed definition

### Important Source Fields

- `title`: feed title
- `start_urls`: listing pages to scan for article links
- `link_selector`: CSS selector that limits which links are considered during discovery
- `include_url_patterns`: regex allowlist for article URLs
- `exclude_url_patterns`: regex denylist for links you do not want
- `auth_profile`: optional auth profile to apply
- `fetch_backend`: `http` or `browser`
- `max_items`: maximum number of items to publish
- `refresh_seconds`: cache freshness window

### Important Auth Profile Fields

- `domains`: domains the profile should apply to
- `cookie_jar_path`: Netscape cookie jar or a file containing a raw `Cookie` header
- `cookie_header`: inline cookie header value
- `browser_profile_path`: dedicated persistent browser profile directory
- `browser_cdp_url`: attach to an already-running Chromium over CDP instead of launching one directly
- `browser_wait_until`, `browser_wait_for_selector`, `browser_settle_seconds`: article-page read timing
- `discovery_browser_wait_until`, `discovery_browser_wait_for_selector`, `discovery_browser_settle_seconds`: listing-page read timing

The included [source-bridge.example.toml](../source-bridge.example.toml) is a worked example, not a special-case format. Add as many sources and auth profiles as you need.

Keep your real `source-bridge.toml` untracked. It commonly contains local file paths, private feed definitions, and references to cookies or browser profiles.

## Choosing A Backend

Use plain HTTP when:

- the site is public
- the listing and article pages are available without login
- the server returns usable HTML without a real browser session

Use the browser backend when:

- the site requires login
- article content only appears after client-side rendering
- you want to keep a persistent dedicated browser profile logged in between runs

Use a CDP-backed browser when:

- the site behaves differently when Playwright launches Chromium itself
- you want a long-lived browser process to stay warm
- you want the bridge to attach to an existing browser instead of competing for the profile lock

Recommended escalation path:

1. Start with plain HTTP.
2. Move to the browser backend if login or rendering requires it.
3. Add CDP only for sites that still behave differently under launched automation.

## Minimal Public-Site Example

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

That publishes:

```text
http://127.0.0.1:8100/synthetic/example-home.xml
```

## FT Example

FT is included because it exercises the hard case: authenticated discovery and article extraction from a browser-backed session.

The intended FT setup is:

1. Create a dedicated browser profile directory, for example `./data/browser-profiles/ft`.
2. Save a cookie file or sign into FT once using that dedicated profile.
3. Keep `fetch_backend = "browser"` and `browser_profile_path` enabled.
4. Use `https://www.ft.com/news-feed` for discovery.
5. Scope discovery to `#site-content a[href]` so header links do not become feed items.
6. Use article waits such as `browser_wait_until = "load"` and `browser_wait_for_selector = "article, main"`.
7. Subscribe FreshRSS to `/synthetic/ft-home.xml`.

If FT or a similar site is sensitive to automation fingerprints, use a dedicated long-lived Chromium over CDP instead of launching a fresh browser process for each request.

Keep FT cookies and profile directories under the ignored `./data/` tree. Do not commit real cookie files, exported headers, or browser profiles.

## Browser Runtime Notes

Browser-backed sources use Playwright with a persistent Chromium or Chrome profile.

For host-native runs:

- install dependencies with `uv sync --extra dev`
- set `browser_channel = "chrome"` if you want to use an installed Google Chrome
- or set `browser_executable_path` if you need an explicit browser binary path

For container runs:

- leave `browser_channel` unset
- use the bundled Playwright Chromium from the Docker image

If a browser-backed source looks stale or wrong, test the same profile headful first, inspect the rendered DOM, and then tighten selectors and waits based on what the page actually renders.

## Environment Variables

| Variable | Required | Purpose | Default |
| --- | --- | --- | --- |
| `DATABASE_PATH` | recommended | SQLite database for synthetic feed state | the Compose examples set it explicitly to a separate bridge DB |
| `SOURCE_BRIDGE_CONFIG_PATH` | yes | TOML config file path | none |
| `SOURCE_BRIDGE_REFRESH_SECONDS` | no | default feed freshness window | `900` |
| `SOURCE_BRIDGE_PREWARM_ENABLED` | no | whether to refresh sources proactively in the background | `true` |
| `SOURCE_BRIDGE_PREWARM_INTERVAL_SECONDS` | no | how often the prewarm loop checks sources | `60` |
| `SOURCE_BRIDGE_ACCESS_TOKEN` | no | optional shared token for protecting bridge endpoints | unset |
| `APP_ALLOWED_HOSTS` | no | comma-separated hostnames allowed by the bridge | unset |
| `HTTP_TIMEOUT_SECONDS` | no | outbound HTTP timeout | `20` |
| `USER_AGENT` | no | outbound user agent for HTTP fetching | `rss-kindle/0.2 (+https://example.invalid; self-hosted personal reader)` |

## Security And Privacy

- keep `source-bridge` on a trusted LAN when possible
- if you expose it, set `SOURCE_BRIDGE_ACCESS_TOKEN` or put it behind your own reverse proxy auth
- keep `source-bridge.toml`, cookies, and browser profiles out of git
- keep browser auth material out of public container images and build contexts
- use dedicated automation browser profiles instead of reusing your daily browser profile
- only use the bridge for sites and accounts you are authorized to access, and review site-specific terms before automating or redistributing content
