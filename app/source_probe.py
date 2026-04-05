from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from app.config import get_settings
from app.db import Database
from app.repository import Repository
from app.source_bridge import SourceBridgeService


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe synthetic source fetch reliability.")
    parser.add_argument("--source", required=True, help="Synthetic source id, for example ft-home")
    parser.add_argument("--limit", type=int, default=5, help="How many discovered article URLs to probe")
    parser.add_argument(
        "--mode",
        choices=("configured", "http"),
        default="configured",
        help="Use the source's configured backend, or force plain HTTP for diagnostics.",
    )
    args = parser.parse_args()

    settings = get_settings()
    repository = Repository(Database(settings.database_path))
    repository.initialize()
    bridge = SourceBridgeService(settings, repository)
    if args.mode == "http":
        results = bridge.probe_http_source(args.source, limit=max(0, args.limit))
    else:
        results = bridge.probe_source(args.source, limit=max(0, args.limit))

    for result in results:
        print(json.dumps(asdict(result), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
