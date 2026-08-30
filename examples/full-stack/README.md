# Full-stack example

This example runs:

- RSS Kindle
- FreshRSS
- the source bridge
- Caddy

Use it when a site has no useful RSS feed or requires a browser session. Otherwise, use the smaller [RSS Kindle with FreshRSS example](../reader-with-freshrss).

## Before you start

1. Change the example hostnames in [`Caddyfile`](Caddyfile). Point both names to the Docker host.
2. Review the volume paths and timezone in [`docker-compose.yml`](docker-compose.yml).
3. Make sure UID and GID `1000` can write to `RSS_KINDLE_DATA_DIR`. Set `RSS_KINDLE_UID` and `RSS_KINDLE_GID` if the directory has a different owner.

The example uses plain HTTP for a trusted local network. Configure HTTPS and set `APP_SECURE_COOKIES=true` before you expose it to the internet.

## Start the stack

Create the bridge configuration and a random access token:

```bash
cp source-bridge.example.toml source-bridge.toml
export RSS_KINDLE_SOURCE_BRIDGE_CONFIG="$(pwd)/source-bridge.toml"
export SOURCE_BRIDGE_ACCESS_TOKEN="$(openssl rand -hex 32)"
```

Set the FreshRSS account name and API password, then start the stack:

```bash
FRESHRSS_USERNAME=reader \
FRESHRSS_API_PASSWORD=replace-me \
docker compose -f examples/full-stack/docker-compose.yml up --build -d
```

Open FreshRSS with its hostname from the Caddyfile. For a new install, create the `reader` account, enable API access, and set its API password to the value used above. RSS Kindle will connect when the account is ready.

Open RSS Kindle with its hostname from the Caddyfile.

FreshRSS can subscribe to a bridge feed at:

```text
http://source-bridge:8100/synthetic/{source_id}.xml
```

Set the feed username to `source-bridge` and the password to `SOURCE_BRIDGE_ACCESS_TOKEN`.

See [Source bridge](../../docs/source-bridge.md) for source configuration and browser authentication. See the main [README](../../README.md) for app authentication, Kindle pairing, and backups.
