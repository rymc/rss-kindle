from datetime import UTC, datetime

from app.article_html import cleanup_kindle_article_html, simplify_html_for_kindle
from app.utils import (
    compact_source_label,
    extract_hacker_news_comments_url,
    format_compact_relative_time,
    format_relative_time,
    hacker_news_destination_host,
    hacker_news_item_id,
    is_comments_only_summary,
    is_kindle_user_agent,
)


def test_is_kindle_user_agent_matches_kindle_and_silk():
    assert is_kindle_user_agent("Mozilla/5.0 (Linux; U; en-us; Kindle 3.0)")
    assert is_kindle_user_agent("Mozilla/5.0 Silk/3.2")
    assert not is_kindle_user_agent("Mozilla/5.0 Safari/605.1.15")


def test_simplify_html_for_kindle_strips_heavy_markup():
    html = """
    <article class="outer">
      <div class="promo"><button>Read distraction-free on Substack</button></div>
      <div class="body">
        <p class="lead">Hello <strong>world</strong>.</p>
        <img src="https://example.com/image.png" alt="image" />
        <div><a href="https://example.com/story" class="cta">Read more</a></div>
      </div>
    </article>
    """

    simplified = simplify_html_for_kindle(html)

    assert "Read distraction-free on Substack" not in simplified
    assert "<img" not in simplified
    assert 'class="' not in simplified
    assert 'href="https://example.com/story"' in simplified
    assert "<strong>world</strong>" in simplified


def test_simplify_html_preserves_media_descriptions_without_image_requests():
    html = """
    <article>
      <figure>
        <img src="https://example.com/chart.png" alt="Quarterly revenue chart" />
        <figcaption>Revenue rose in every region.</figcaption>
      </figure>
      <p>Results follow the chart.</p>
      <img src="https://example.com/map.png" alt="Map of the affected area" />
      <picture>
        <source srcset="https://example.com/device.webp" />
        <img src="https://example.com/device.png" alt="The new reading device" />
      </picture>
    </article>
    """

    simplified = simplify_html_for_kindle(html)

    assert "<img" not in simplified
    assert "https://example.com/chart.png" not in simplified
    assert "Figure: Revenue rose in every region." in simplified
    assert "Image: Map of the affected area" in simplified
    assert "Image: The new reading device" in simplified


def test_inline_image_description_stays_inside_its_paragraph():
    simplified = simplify_html_for_kindle(
        """
        <article>
          <p>Before <img src="chart.png" alt="sales chart"> after.</p>
          <p>Before <picture><img src="map.png" alt="route map"></picture> after.</p>
        </article>
        """
    )

    assert "<p>Before <em>Image: sales chart</em> after.</p>" in simplified
    assert "<p>Before <em>Image: route map</em> after.</p>" in simplified
    assert "<p>Before <p>" not in simplified


def test_format_relative_time_uses_reader_friendly_labels():
    reference = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)

    assert format_relative_time("2026-03-29T11:59:00+00:00", reference) == "1 min ago"
    assert format_relative_time("2026-03-29T10:00:00+00:00", reference) == "2 hours ago"
    assert format_relative_time("2026-03-27T12:00:00+00:00", reference) == "2 days ago"


def test_format_compact_relative_time_uses_short_labels():
    reference = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)

    assert format_compact_relative_time(
        "2026-03-29T11:59:00+00:00", reference
    ) == "1m"
    assert format_compact_relative_time(
        "2026-03-29T10:00:00+00:00", reference
    ) == "2h"
    assert format_compact_relative_time(
        "2026-03-27T12:00:00+00:00", reference
    ) == "2d"
    assert format_compact_relative_time(
        "2026-03-29T12:02:00+00:00", reference
    ) == "in 2m"


def test_compact_source_label_trims_verbose_feed_titles():
    assert (
        compact_source_label(
            "MacRumors: Mac News and Rumors - Front Page",
            "https://www.macrumors.com",
        )
        == "MacRumors"
    )
    assert compact_source_label(None, "https://news.ycombinator.com") == "Ycombinator"


def test_extract_hacker_news_comments_url_prefers_hn_discussion_link():
    comments_url = extract_hacker_news_comments_url(
        summary_html='<p><a href="https://news.ycombinator.com/item?id=43849891">Comments</a></p>',
        content_html=None,
        entry_url="https://example.com/story",
        feed_site_url="https://news.ycombinator.com/",
    )

    assert comments_url == "https://news.ycombinator.com/item?id=43849891"


def test_hacker_news_helpers_identify_discussion_and_destination():
    assert (
        hacker_news_item_id("https://news.ycombinator.com/item?id=43849891")
        == 43849891
    )
    assert hacker_news_item_id("https://example.com/item?id=43849891") is None
    assert hacker_news_item_id("https://news.ycombinator.com/item?id=bad") is None
    assert (
        hacker_news_destination_host(
            "https://www.github.com/example/project",
            "https://news.ycombinator.com/",
        )
        == "github.com"
    )
    assert (
        hacker_news_destination_host(
            "https://news.ycombinator.com/item?id=1",
            "https://news.ycombinator.com/",
        )
        is None
    )
    assert (
        hacker_news_destination_host(
            "https://[/invalid",
            "https://news.ycombinator.com/",
        )
        is None
    )


def test_is_comments_only_summary_matches_comment_only_previews():
    assert is_comments_only_summary("Comments")
    assert is_comments_only_summary("<p>Discussion</p>")
    assert not is_comments_only_summary("A longer preview paragraph")


def test_cleanup_kindle_article_html_removes_duplicate_header_blocks():
    html = """
    <article>
      <h1>Apple Preparing 'Most Significant Overhaul in the iPhone's History'</h1>
      <p>MacRumors: Mac News and Rumors - Front Page</p>
      <p>2026-03-29 15:18 UTC</p>
      <p><a href="https://www.macrumors.com/story">Open the source article</a></p>
      <p>Actual opening paragraph.</p>
    </article>
    """

    cleaned = cleanup_kindle_article_html(
        html,
        item_title="Apple Preparing 'Most Significant Overhaul in the iPhone's History'",
        source_label="MacRumors",
        feed_title="MacRumors: Mac News and Rumors - Front Page",
        source_url="https://www.macrumors.com/story",
    )

    assert "Mac News and Rumors - Front Page" not in cleaned
    assert "2026-03-29 15:18 UTC" not in cleaned
    assert "Open the source article" not in cleaned
    assert cleaned.count("Apple Preparing") == 0
    assert "Actual opening paragraph." in cleaned


def test_cleanup_kindle_article_html_stops_at_first_real_content():
    html = """
    <article>
      <p>By Jane Reporter</p>
      <p>This is the first real paragraph and it should stay in place.</p>
      <p>Second paragraph.</p>
    </article>
    """

    cleaned = cleanup_kindle_article_html(
        html,
        item_title="Story",
        source_label="Example",
        feed_title="Example Feed",
        source_url="https://example.com/story",
    )

    assert "By Jane Reporter" not in cleaned
    assert "This is the first real paragraph and it should stay in place." in cleaned
    assert "Second paragraph." in cleaned


def test_cleanup_kindle_article_html_removes_non_matching_lead_heading():
    html = """
    <article>
      <h1>Edit PDFs Easily and Securely</h1>
      <p>This is the first real paragraph and it should remain.</p>
    </article>
    """

    cleaned = cleanup_kindle_article_html(
        html,
        item_title="Show HN: BreezePDF – Free, in-browser PDF editor",
        source_label="Hacker News",
        feed_title="Hacker News",
        source_url="https://breezepdf.com/?v=3",
    )

    assert "Edit PDFs Easily and Securely" not in cleaned
    assert "This is the first real paragraph and it should remain." in cleaned
