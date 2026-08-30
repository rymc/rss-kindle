from __future__ import annotations

import argparse
import math
import os
import statistics
from dataclasses import dataclass
from urllib.parse import urljoin


@dataclass(frozen=True)
class PageTurnSample:
    handler_ms: float
    settle_ms: float
    mutations: int
    layout_count: float
    style_count: float
    layout_style_ms: float
    main_thread_ms: float


@dataclass(frozen=True)
class BrowserSample:
    ttfb_ms: float
    first_contentful_paint_ms: float
    load_ms: float
    transferred_bytes: int
    encoded_bytes: int
    request_count: int
    dom_nodes: int
    layout_shift: float
    long_task_ms: float
    horizontal_overflow: bool
    page_turn: PageTurnSample | None


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _read_sample(
    page,
    *,
    page_turn: PageTurnSample | None = None,
) -> BrowserSample:
    values = page.evaluate(
        """() => {
          const navigation = performance.getEntriesByType("navigation")[0];
          const resources = performance.getEntriesByType("resource");
          const paint = performance.getEntriesByType("paint");
          const fcp = paint.find((entry) => entry.name === "first-contentful-paint");
          return {
            ttfb: navigation.responseStart,
            fcp: fcp ? fcp.startTime : 0,
            load: navigation.loadEventEnd,
            transferred: navigation.transferSize + resources.reduce(
              (total, entry) => total + (entry.transferSize || 0), 0
            ),
            encoded: navigation.encodedBodySize + resources.reduce(
              (total, entry) => total + (entry.encodedBodySize || 0), 0
            ),
            requests: 1 + resources.length,
            nodes: document.getElementsByTagName("*").length,
            layoutShift: window.__rssKindleVitals.layoutShift,
            longTaskMs: window.__rssKindleVitals.longTaskMs,
            horizontalOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth
          };
        }"""
    )
    return BrowserSample(
        ttfb_ms=float(values["ttfb"]),
        first_contentful_paint_ms=float(values["fcp"]),
        load_ms=float(values["load"]),
        transferred_bytes=int(values["transferred"]),
        encoded_bytes=int(values["encoded"]),
        request_count=int(values["requests"]),
        dom_nodes=int(values["nodes"]),
        layout_shift=float(values["layoutShift"]),
        long_task_ms=float(values["longTaskMs"]),
        horizontal_overflow=bool(values["horizontalOverflow"]),
        page_turn=page_turn,
    )


def _cdp_performance_metrics(cdp) -> dict[str, float]:
    return {
        metric["name"]: float(metric["value"])
        for metric in cdp.send("Performance.getMetrics")["metrics"]
    }


def _measure_page_turn(page, cdp) -> PageTurnSample | None:
    """Return browser-work proxies for one turn, not e-ink refresh metrics."""
    before = _cdp_performance_metrics(cdp)
    value = page.evaluate(
        """async () => {
          const button = document.querySelector('[data-page-turn="1"]');
          if (!button || button.disabled) return null;
          const controls = button.closest('[data-page-mode]');
          if (!controls || controls.hidden) return null;
          const isStream = controls?.getAttribute('data-page-mode') === 'stream';
          if (isStream) {
            const cards = document.querySelectorAll('[data-entry-card]');
            const visibleCards = document.querySelectorAll('.is-stream-page-visible');
            if (cards.length <= visibleCards.length) return null;
          } else if (
            document.documentElement.scrollHeight
              <= document.documentElement.clientHeight + 24
          ) {
            return null;
          }
          let mutationCount = 0;
          const observer = new MutationObserver((records) => {
            mutationCount += records.length;
          });
          observer.observe(document.documentElement, {
            attributes: true,
            characterData: true,
            childList: true,
            subtree: true
          });
          const startedAt = performance.now();
          button.click();
          const handlerFinishedAt = performance.now();
          await new Promise((resolve) => requestAnimationFrame(
            () => requestAnimationFrame(resolve)
          ));
          mutationCount += observer.takeRecords().length;
          observer.disconnect();
          return {
            handler: handlerFinishedAt - startedAt,
            settle: performance.now() - startedAt,
            mutations: mutationCount
          };
        }"""
    )
    if value is None:
        return None
    after = _cdp_performance_metrics(cdp)

    def delta(name: str) -> float:
        return max(0.0, after.get(name, 0.0) - before.get(name, 0.0))

    return PageTurnSample(
        handler_ms=float(value["handler"]),
        settle_ms=float(value["settle"]),
        mutations=int(value["mutations"]),
        layout_count=delta("LayoutCount"),
        style_count=delta("RecalcStyleCount"),
        layout_style_ms=(delta("LayoutDuration") + delta("RecalcStyleDuration"))
        * 1000,
        main_thread_ms=delta("TaskDuration") * 1000,
    )


def _summary(label: str, values: list[float], unit: str = "ms") -> str:
    return (
        f"{label}: median {statistics.median(values):.1f} {unit}, "
        f"p95 {_percentile(values, 0.95):.1f} {unit}, max {max(values):.1f} {unit}"
    )


def _parse_args() -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    parser = argparse.ArgumentParser(
        description="Measure RSS Kindle rendering under a throttled Chromium profile."
    )
    parser.add_argument("url", help="Base URL, such as https://reader.example.com")
    parser.add_argument(
        "--path", default="/", help="Reader path to measure (default: /)"
    )
    parser.add_argument(
        "--requests", type=int, default=5, help="Measured page loads (default: 5)"
    )
    parser.add_argument(
        "--warmup", type=int, default=1, help="Warm page loads (default: 1)"
    )
    parser.add_argument(
        "--width", type=int, default=600, help="Viewport width (default: 600)"
    )
    parser.add_argument(
        "--height", type=int, default=800, help="Viewport height (default: 800)"
    )
    parser.add_argument(
        "--cpu-rate", type=float, default=6, help="CPU slowdown factor (default: 6)"
    )
    parser.add_argument(
        "--latency-ms",
        type=int,
        default=150,
        help="Added network latency (default: 150)",
    )
    parser.add_argument(
        "--download-kbps",
        type=int,
        default=1000,
        help="Download rate (default: 1000 Kbit/s)",
    )
    parser.add_argument(
        "--upload-kbps", type=int, default=500, help="Upload rate (default: 500 Kbit/s)"
    )
    parser.add_argument(
        "--cold",
        action="store_true",
        help="Clear the browser cache before each measured load",
    )
    args = parser.parse_args()
    return parser, args


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.requests < 1 or args.warmup < 0:
        parser.error("--requests must be at least 1 and --warmup cannot be negative")
    if min(args.width, args.height, args.download_kbps, args.upload_kbps) < 1:
        parser.error("viewport and network rates must be positive")
    if args.cpu_rate < 1 or args.latency_ms < 0:
        parser.error(
            "--cpu-rate must be at least 1 and --latency-ms cannot be negative"
        )


def _load_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise SystemExit(
            "Install the browser extra first: uv sync --extra browser"
        ) from exc
    return sync_playwright


def _configure_browser(context, page, args: argparse.Namespace):
    context.add_init_script(
        """
        window.__rssKindleVitals = {layoutShift: 0, longTaskMs: 0};
        try {
          new PerformanceObserver((list) => list.getEntries().forEach((entry) => {
            if (!entry.hadRecentInput) window.__rssKindleVitals.layoutShift += entry.value;
          })).observe({type: "layout-shift", buffered: true});
          new PerformanceObserver((list) => list.getEntries().forEach((entry) => {
            window.__rssKindleVitals.longTaskMs += entry.duration;
          })).observe({type: "longtask", buffered: true});
        } catch (error) {}
        """
    )
    cdp = context.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.send("Performance.enable")
    cdp.send("Emulation.setCPUThrottlingRate", {"rate": args.cpu_rate})
    cdp.send(
        "Network.emulateNetworkConditions",
        {
            "offline": False,
            "latency": args.latency_ms,
            "downloadThroughput": args.download_kbps * 1024 / 8,
            "uploadThroughput": args.upload_kbps * 1024 / 8,
            "connectionType": "wifi",
        },
    )
    return cdp


def _login(page, base_url: str, parser: argparse.ArgumentParser) -> None:
    username = os.getenv("RSS_KINDLE_BENCHMARK_USERNAME")
    password = os.getenv("RSS_KINDLE_BENCHMARK_PASSWORD")
    if not username and not password:
        return
    if not username or not password:
        parser.error(
            "set both RSS_KINDLE_BENCHMARK_USERNAME and RSS_KINDLE_BENCHMARK_PASSWORD"
        )
    page.goto(urljoin(base_url, "login"), wait_until="load")
    page.locator('input[name="username"]').fill(username)
    page.locator('input[name="password"]').fill(password)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_load_state("load")
    if "/login" in page.url:
        raise SystemExit("Login failed.")


def _collect_samples(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    base_url: str,
    target_url: str,
) -> list[BrowserSample]:
    sync_playwright = _load_playwright()
    samples: list[BrowserSample] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": args.width, "height": args.height}
        )
        page = context.new_page()
        cdp = _configure_browser(context, page, args)
        _login(page, base_url, parser)

        for _ in range(args.warmup):
            page.goto(target_url, wait_until="load")

        for _ in range(args.requests):
            if args.cold:
                cdp.send("Network.clearBrowserCache")
            page.goto(target_url, wait_until="load")
            # Let client-side readers finish their initial view changes before
            # recording DOM and interaction work.
            page.wait_for_timeout(500)
            page_turn = _measure_page_turn(page, cdp)
            samples.append(
                _read_sample(
                    page,
                    page_turn=page_turn,
                )
            )

        context.close()
        browser.close()
    return samples


def _print_report(
    args: argparse.Namespace, target_url: str, samples: list[BrowserSample]
) -> None:
    print(f"URL: {target_url}")
    print(
        f"Profile: {args.width}x{args.height}, {args.cpu_rate:g}x CPU slowdown, "
        f"{args.latency_ms} ms latency, {args.download_kbps} Kbit/s down"
    )
    print(
        f"Loads: {len(samples)} after {args.warmup} warmup; cache: {'cold' if args.cold else 'warm'}"
    )
    print(_summary("TTFB", [sample.ttfb_ms for sample in samples]))
    print(
        _summary(
            "First contentful paint",
            [sample.first_contentful_paint_ms for sample in samples],
        )
    )
    print(_summary("Load", [sample.load_ms for sample in samples]))
    print(
        f"Encoded page and assets: median {statistics.median(sample.encoded_bytes for sample in samples):.0f} bytes"
    )
    print(
        f"Transferred per load: median {statistics.median(sample.transferred_bytes for sample in samples):.0f} bytes"
    )
    print(
        f"DOM nodes: median {statistics.median(sample.dom_nodes for sample in samples):.0f}"
    )
    print(
        f"Performance entries: median {statistics.median(sample.request_count for sample in samples):.0f}"
    )
    print(f"Layout shift: max {max(sample.layout_shift for sample in samples):.4f}")
    print(f"Long tasks: max {max(sample.long_task_ms for sample in samples):.1f} ms")
    page_turns = [sample.page_turn for sample in samples if sample.page_turn]
    if page_turns:
        print(
            _summary(
                "Page-turn handler (Chromium proxy)",
                [turn.handler_ms for turn in page_turns],
            )
        )
        print(
            _summary(
                "Page-turn main-thread work (Chromium proxy)",
                [turn.main_thread_ms for turn in page_turns],
            )
        )
        print(
            _summary(
                "Page-turn layout and style work (Chromium proxy)",
                [turn.layout_style_ms for turn in page_turns],
            )
        )
        print(
            "Page-turn layout/style passes: median "
            f"{statistics.median(turn.layout_count for turn in page_turns):.0f}/"
            f"{statistics.median(turn.style_count for turn in page_turns):.0f}"
        )
        print(
            "Page-turn DOM mutations: median "
            f"{statistics.median(turn.mutations for turn in page_turns):.0f}"
        )
        print(
            _summary(
                "Two-frame page-turn settle (Chromium proxy)",
                [turn.settle_ms for turn in page_turns],
            )
        )
    print(
        f"Horizontal overflow: {'yes' if any(sample.horizontal_overflow for sample in samples) else 'no'}"
    )


def main() -> None:
    parser, args = _parse_args()
    _validate_args(parser, args)

    base_url = args.url.rstrip("/") + "/"
    target_url = urljoin(base_url, args.path.lstrip("/"))
    samples = _collect_samples(args, parser, base_url, target_url)
    _print_report(args, target_url, samples)


if __name__ == "__main__":
    main()
