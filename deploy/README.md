Environment-specific deployment overlays should stay out of git.

Use the committed root `docker-compose.yml` and the examples under `examples/`
as the portable base.

Keep real deployment files such as:

- host-specific Compose overlays
- reverse-proxy configs with real domains
- real `.env` files
- real `source-bridge.toml` files
- cookie/header files
- browser-profile directories
- LAN IPs or machine inventory notes
- local operator notes such as `AGENTS.local.md`

in a separate private ops repo or in a gitignored directory such as:

- `deploy/private/`
- `deploy/local/`
