# Reader + FreshRSS Example

This directory is the middle deployment path:

- `rss-kindle`
- FreshRSS

Use it when:

- you want to self-host FreshRSS and the Kindle reader together
- you do not need `source-bridge` yet
- you want something simpler than the full stack example

Follow [the reader and FreshRSS setup in the main README](../../README.md#path-b-you-want-to-self-host-freshrss-too) for startup commands and verification URLs.

Review [`docker-compose.yml`](docker-compose.yml) before using it, especially the volume paths, timezone, and FreshRSS credentials for `rss-kindle`.

The reader container uses the numeric user and group set by `RSS_KINDLE_UID` and `RSS_KINDLE_GID`. Both default to `1000`. Make sure that user can write to `RSS_KINDLE_DATA_DIR`.

If you later need bridged sites, use the [full-stack example](../full-stack).
