# Full Stack Example

This directory is the largest deployment path and a superset of `examples/reader-with-freshrss`.

It is a reference deployment for running all services together on one Docker network:

- `rss-kindle`
- `source-bridge`
- FreshRSS
- Caddy

Use it when you need bridged sites. If you only want the reader and FreshRSS together, start with [../reader-with-freshrss](../reader-with-freshrss) instead.

Follow [the full-stack setup in the main README](../../README.md#path-c-you-want-the-full-stack) for startup commands and verification URLs.

Before using this stack, review and replace the defaults in [`docker-compose.yml`](docker-compose.yml) and [`Caddyfile`](Caddyfile), especially volume paths, timezone, hostnames, FreshRSS credentials, and the `source-bridge` config path.

The app containers use the numeric user and group set by `RSS_KINDLE_UID` and `RSS_KINDLE_GID`. Both default to `1000`. Make sure that user can write to `RSS_KINDLE_DATA_DIR`. The reader uses the small image. The source bridge uses the separate browser image.

If you do not need bridged sites, use the smaller [reader and FreshRSS example](../reader-with-freshrss).
