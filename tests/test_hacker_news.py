import threading
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

from app.config import Settings
from app.hacker_news import (
    HackerNewsClient,
    HackerNewsDiscussion,
    HackerNewsError,
)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, items):
        self.items = items
        self.calls: list[int] = []
        self.closed = False
        self._lock = threading.Lock()

    def get(self, url: str):
        item_id = int(Path(urlparse(url).path).stem)
        with self._lock:
            self.calls.append(item_id)
            item = self.items.get(item_id)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)

    def close(self):
        self.closed = True


class BlockingCommentClient(FakeClient):
    def __init__(self, items):
        super().__init__(items)
        self.two_comments_started = threading.Event()
        self.release_comments = threading.Event()
        self._active_comments = 0

    def get(self, url: str):
        item_id = int(Path(urlparse(url).path).stem)
        if item_id != 1:
            with self._lock:
                self._active_comments += 1
                if self._active_comments >= 2:
                    self.two_comments_started.set()
            self.release_comments.wait(timeout=2)
        return super().get(url)


def settings(tmp_path: Path) -> Settings:
    return Settings(
        app_name="RSS Kindle",
        base_dir=Path(__file__).resolve().parent.parent,
        database_path=tmp_path / "reader.db",
        http_timeout_seconds=5,
        user_agent="test-agent",
        max_stream_items=15,
        metadata_cache_seconds=60,
        freshrss_api_url=None,
        freshrss_username=None,
        freshrss_api_password=None,
    )


def discussion_items():
    return {
        1: {
            "id": 1,
            "type": "story",
            "by": "story-author",
            "time": 1_700_000_000,
            "score": 42,
            "descendants": 4,
            "title": "A useful story",
            "url": "https://www.example.com/article",
            "kids": [2, 3],
        },
        2: {
            "id": 2,
            "type": "comment",
            "by": "alice",
            "time": 1_700_000_100,
            "text": '<p>First <strong>comment</strong>.</p><script>bad()</script>',
            "kids": [4],
        },
        3: {
            "id": 3,
            "type": "comment",
            "deleted": True,
            "kids": [5],
        },
        4: {
            "id": 4,
            "type": "comment",
            "by": "bob",
            "time": 1_700_000_200,
            "text": "<p>A reply to Alice.</p>",
        },
        5: {
            "id": 5,
            "type": "comment",
            "by": "carol",
            "time": 1_700_000_300,
            "text": "<p>A reply below the deleted comment.</p>",
        },
    }


def test_discussion_fetches_sanitizes_threads_and_caches(tmp_path: Path):
    fake = FakeClient(discussion_items())
    client = HackerNewsClient(
        settings(tmp_path),
        client_factory=lambda: fake,
        max_comments=10,
    )

    discussion = client.get_discussion(1)
    cached = client.get_discussion(1)

    assert cached is discussion
    assert discussion.title == "A useful story"
    assert discussion.destination_host == "example.com"
    assert discussion.comment_count == 4
    assert discussion.is_partial is False
    assert [comment.id for comment in discussion.comments] == [2, 4, 3, 5]
    assert [comment.depth for comment in discussion.comments] == [0, 1, 0, 1]
    assert discussion.comments[1].parent_author == "alice"
    assert discussion.comments[2].author == "Deleted"
    assert discussion.comments[2].is_deleted is True
    assert "Deleted comment" in discussion.comments[2].html
    assert "<script" not in discussion.comments[0].html
    assert "<strong>comment</strong>" in discussion.comments[0].html
    assert sorted(fake.calls) == [1, 2, 3, 4, 5]

    client.close()
    assert fake.closed is True


def test_discussion_limit_reports_partial_thread(tmp_path: Path):
    fake = FakeClient(discussion_items())
    client = HackerNewsClient(
        settings(tmp_path),
        client_factory=lambda: fake,
        max_comments=2,
    )

    discussion = client.get_discussion(1)

    assert [comment.id for comment in discussion.comments] == [2, 3]
    assert discussion.is_partial is True
    assert sorted(fake.calls) == [1, 2, 3]
    client.close()


def test_discussion_fetches_independent_comments_in_parallel(tmp_path: Path):
    fake = BlockingCommentClient(discussion_items())
    client = HackerNewsClient(
        settings(tmp_path),
        client_factory=lambda: fake,
        max_comments=2,
    )
    result: list[HackerNewsDiscussion] = []
    worker = threading.Thread(target=lambda: result.append(client.get_discussion(1)))
    worker.start()

    comments_started_in_parallel = fake.two_comments_started.wait(timeout=1)
    fake.release_comments.set()
    worker.join(timeout=2)

    assert comments_started_in_parallel is True
    assert worker.is_alive() is False
    assert [comment.id for comment in result[0].comments] == [2, 3]
    client.close()


def test_expired_discussion_falls_back_to_stale_cache(tmp_path: Path):
    now = [0.0]
    items = discussion_items()
    fake = FakeClient(items)
    client = HackerNewsClient(
        settings(tmp_path),
        client_factory=lambda: fake,
        cache_seconds=1,
        clock=lambda: now[0],
    )
    fresh = client.get_discussion(1)
    now[0] = 2.0
    items[1] = httpx.ConnectError("offline")

    stale = client.get_discussion(1)
    calls_after_failure = list(fake.calls)
    cached_stale = client.get_discussion(1)

    assert fresh.is_stale is False
    assert stale.is_stale is True
    assert stale.comments == fresh.comments
    assert cached_stale is stale
    assert fake.calls == calls_after_failure
    client.close()


def test_discussion_stops_at_fetch_budget_and_returns_partial_thread(
    tmp_path: Path,
):
    now = [-1.0]

    def clock():
        now[0] += 1.0
        return now[0]

    items = {
        1: {
            "id": 1,
            "type": "story",
            "title": "Deep thread",
            "descendants": 2,
            "kids": [2],
        },
        2: {"id": 2, "type": "comment", "text": "First", "kids": [3]},
        3: {"id": 3, "type": "comment", "text": "Second"},
    }
    fake = FakeClient(items)
    client = HackerNewsClient(
        settings(tmp_path),
        client_factory=lambda: fake,
        fetch_budget_seconds=2,
        clock=clock,
    )

    discussion = client.get_discussion(1)

    assert [comment.id for comment in discussion.comments] == [2]
    assert discussion.is_partial is True
    assert fake.calls == [1, 2]
    client.close()


def test_missing_story_raises_clear_error(tmp_path: Path):
    fake = FakeClient({1: None})
    client = HackerNewsClient(
        settings(tmp_path),
        client_factory=lambda: fake,
    )

    with pytest.raises(HackerNewsError, match="not found"):
        client.get_discussion(1)
    client.close()
