# Full Stack Example

This directory is the largest deployment path and a superset of `examples/reader-with-freshrss`.

It is a bundled reference deployment for running all of the repo's moving parts together on one Docker network:

- `rss-kindle`
- `source-bridge`
- FreshRSS
- Caddy

Use it when you need bridged sites. If you only want the reader and FreshRSS together, start with [../reader-with-freshrss](../reader-with-freshrss) instead.

The main README contains the primary setup flow and verification URLs for this path. This file only explains what this example is for.

Before using this stack, review and replace the defaults in [`docker-compose.yml`](docker-compose.yml) and [`Caddyfile`](Caddyfile), especially volume paths, timezone, hostnames, FreshRSS credentials, and the `source-bridge` config path.

If you do not need bridged sites, you can still use this stack and simply ignore `source-bridge`.
