from __future__ import annotations

import argparse
import math
import os
import statistics
import time
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class Sample:
    elapsed_ms: float
    size_bytes: int
    server_timing: str


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _fetch(client: httpx.Client, path: str) -> Sample:
    started_at = time.perf_counter()
    response = client.get(path)
    elapsed_ms = (time.perf_counter() - started_at) * 1000
    response.raise_for_status()
    return Sample(
        elapsed_ms=elapsed_ms,
        size_bytes=len(response.content),
        server_timing=response.headers.get("server-timing", ""),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure RSS Kindle response time and size.")
    parser.add_argument("url", help="Base URL, such as https://reader.example.com")
    parser.add_argument("--path", default="/", help="Reader path to measure (default: /)")
    parser.add_argument("--requests", type=int, default=20, help="Measured requests (default: 20)")
    parser.add_argument("--warmup", type=int, default=2, help="Warmup requests (default: 2)")
    args = parser.parse_args()

    if args.requests < 1 or args.warmup < 0:
        parser.error("--requests must be at least 1 and --warmup cannot be negative")

    username = os.getenv("RSS_KINDLE_BENCHMARK_USERNAME")
    password = os.getenv("RSS_KINDLE_BENCHMARK_PASSWORD")
    with httpx.Client(base_url=args.url.rstrip("/"), follow_redirects=True, timeout=30) as client:
        if username or password:
            if not username or not password:
                parser.error("set both RSS_KINDLE_BENCHMARK_USERNAME and RSS_KINDLE_BENCHMARK_PASSWORD")
            login = client.post(
                "/login",
                data={"username": username, "password": password, "next_path": args.path},
            )
            login.raise_for_status()
            if login.url.path == "/login":
                raise SystemExit("Login failed.")

        for _ in range(args.warmup):
            _fetch(client, args.path)
        samples = [_fetch(client, args.path) for _ in range(args.requests)]

    times = [sample.elapsed_ms for sample in samples]
    sizes = [sample.size_bytes for sample in samples]
    print(f"URL: {args.url.rstrip('/')}{args.path}")
    print(f"Requests: {len(samples)} after {args.warmup} warmup")
    print(
        "Client time: "
        f"min {min(times):.1f} ms, median {statistics.median(times):.1f} ms, "
        f"p95 {_percentile(times, 0.95):.1f} ms, max {max(times):.1f} ms"
    )
    print(f"Response body: median {statistics.median(sizes):.0f} bytes")
    if samples[-1].server_timing:
        print(f"Latest Server-Timing: {samples[-1].server_timing}")


if __name__ == "__main__":
    main()
