# RSS Kindle with FreshRSS

This example runs RSS Kindle and FreshRSS on one Docker network. Use it when you want to host both services but do not need the source bridge.

## Before you start

Review [`docker-compose.yml`](docker-compose.yml). In particular, set the persistent data paths and timezone for your host.

The RSS Kindle container uses UID and GID `1000` by default. Make sure that user can write to `RSS_KINDLE_DATA_DIR`, or set `RSS_KINDLE_UID` and `RSS_KINDLE_GID`.

## Start the services

Start FreshRSS first:

```bash
docker compose -f examples/reader-with-freshrss/docker-compose.yml up -d freshrss
```

Open [http://127.0.0.1:8081](http://127.0.0.1:8081) and complete the FreshRSS setup. Enable API access for the account that RSS Kindle will use.

Then start RSS Kindle with the FreshRSS account name and API password:

```bash
FRESHRSS_USERNAME=reader \
FRESHRSS_API_PASSWORD=replace-me \
docker compose -f examples/reader-with-freshrss/docker-compose.yml up --build -d rss-kindle
```

Open RSS Kindle at [http://127.0.0.1:8000](http://127.0.0.1:8000).

See the main [README](../../README.md) for app authentication, Kindle pairing, and other settings. If you need browser-backed or synthetic feeds, use the [full-stack example](../full-stack).
