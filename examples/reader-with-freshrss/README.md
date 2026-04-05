# Reader + FreshRSS Example

This directory is the middle deployment path:

- `rss-kindle`
- FreshRSS

Use it when:

- you want to self-host FreshRSS and the Kindle reader together
- you do not need `source-bridge` yet
- you want something simpler than the full stack example

The main README contains the primary setup flow and verification URLs for this path. This file only explains what this example is for.

Review [`docker-compose.yml`](docker-compose.yml) before using it, especially the volume paths, timezone, and FreshRSS credentials for `rss-kindle`.

If you later need bridged sites, move up to the full stack example in [../full-stack](../full-stack).
