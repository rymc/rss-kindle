from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from app.db import Database
from app.mutations import DurableMutationQueue
from app.repository import Repository


class BlockingFreshRSS:
    def __init__(self):
        self.local_states: list[tuple[str, tuple[str, ...], bool]] = []
        self.sent_states: list[tuple[str, tuple[str, ...], bool]] = []
        self.confirmed_states: list[tuple[str, tuple[str, ...], bool]] = []
        self.send_started = threading.Event()
        self.release_send = threading.Event()

    def apply_local_state(self, state_kind, entry_ids, *, enabled):
        self.local_states.append((state_kind, tuple(entry_ids), enabled))

    def send_state(self, state_kind, entry_ids, *, enabled):
        self.sent_states.append((state_kind, tuple(entry_ids), enabled))
        self.send_started.set()
        self.release_send.wait(timeout=2)

    def confirm_local_state(self, state_kind, entry_ids, *, enabled):
        self.confirmed_states.append((state_kind, tuple(entry_ids), enabled))


class FlakyFreshRSS(BlockingFreshRSS):
    def __init__(self):
        super().__init__()
        self.release_send.set()
        self.attempts = 0

    def send_state(self, state_kind, entry_ids, *, enabled):
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("temporary failure")
        super().send_state(state_kind, entry_ids, enabled=enabled)


class ConcurrentBlockingFreshRSS(BlockingFreshRSS):
    def __init__(self):
        super().__init__()
        self.second_send_started = threading.Event()
        self.active_sends = 0
        self.maximum_active_sends = 0
        self._send_lock = threading.Lock()

    def send_state(self, state_kind, entry_ids, *, enabled):
        with self._send_lock:
            self.sent_states.append((state_kind, tuple(entry_ids), enabled))
            self.active_sends += 1
            self.maximum_active_sends = max(
                self.maximum_active_sends,
                self.active_sends,
            )
            if len(self.sent_states) == 1:
                self.send_started.set()
            else:
                self.second_send_started.set()
        try:
            self.release_send.wait(timeout=2)
        finally:
            with self._send_lock:
                self.active_sends -= 1


class FreshRSSProcessClient(BlockingFreshRSS):
    def __init__(self, upstream: ConcurrentBlockingFreshRSS):
        super().__init__()
        self.upstream = upstream

    def send_state(self, state_kind, entry_ids, *, enabled):
        self.upstream.send_state(state_kind, entry_ids, enabled=enabled)


class BlockingApplyFreshRSS(BlockingFreshRSS):
    def __init__(self):
        super().__init__()
        self.release_send.set()
        self.apply_started = threading.Event()
        self.release_apply = threading.Event()
        self.state: dict[tuple[str, str], tuple[bool, str]] = {}

    def apply_local_state(self, state_kind, entry_ids, *, enabled):
        self.apply_started.set()
        if not self.release_apply.wait(timeout=2):
            raise RuntimeError("local state application timed out")
        super().apply_local_state(state_kind, entry_ids, enabled=enabled)
        for entry_id in entry_ids:
            self.state[(state_kind, entry_id)] = (enabled, "pending")

    def confirm_local_state(self, state_kind, entry_ids, *, enabled):
        super().confirm_local_state(state_kind, entry_ids, enabled=enabled)
        for entry_id in entry_ids:
            key = (state_kind, entry_id)
            if self.state.get(key) == (enabled, "pending"):
                self.state[key] = (enabled, "confirmed")


class AlwaysFailingFreshRSS(BlockingFreshRSS):
    def __init__(self):
        super().__init__()
        self.attempt_times: list[float] = []
        self._attempt_condition = threading.Condition()

    def send_state(self, state_kind, entry_ids, *, enabled):
        with self._attempt_condition:
            self.attempt_times.append(time.monotonic())
            self._attempt_condition.notify_all()
        raise RuntimeError("FreshRSS is unavailable")

    def wait_for_attempts(self, count: int, *, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._attempt_condition:
            while len(self.attempt_times) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._attempt_condition.wait(timeout=remaining)
        return True


class FaultInjectingRepository(Repository):
    def __init__(self, database: Database):
        super().__init__(database)
        self.fail_next_list = False
        self.fail_next_acknowledge = False

    def list_pending_mutations(self, *, limit=100):
        if (
            self.fail_next_list
            and threading.current_thread().name == "rss-kindle-mutations"
        ):
            self.fail_next_list = False
            raise sqlite3.OperationalError("database is locked")
        return super().list_pending_mutations(limit=limit)

    def acknowledge_mutations(self, mutations):
        if self.fail_next_acknowledge:
            self.fail_next_acknowledge = False
            raise sqlite3.OperationalError("database is locked")
        return super().acknowledge_mutations(mutations)


class PausingReconcileRepository(Repository):
    def __init__(self, database: Database):
        super().__init__(database)
        self.reconcile_started = threading.Event()
        self.release_reconcile = threading.Event()
        self._pause_lock = threading.Lock()
        self._pause_next_reconcile = True

    def list_pending_mutations(self, *, limit=100):
        should_pause = False
        if (
            limit is None
            and threading.current_thread().name == "rss-kindle-mutations"
        ):
            with self._pause_lock:
                should_pause = self._pause_next_reconcile
                self._pause_next_reconcile = False
        if should_pause:
            self.reconcile_started.set()
            if not self.release_reconcile.wait(timeout=2):
                raise RuntimeError("reconciliation did not resume")
        return super().list_pending_mutations(limit=limit)


class OwnerObservedRepository(Repository):
    def __init__(self, database: Database):
        super().__init__(database)
        self.worker_listed = threading.Event()

    def list_pending_mutations(self, *, limit=100):
        pending = super().list_pending_mutations(limit=limit)
        if (
            limit is not None
            and threading.current_thread().name == "rss-kindle-mutations"
        ):
            self.worker_listed.set()
        return pending


def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Condition did not become true before the deadline.")


def test_mutation_is_durable_and_local_before_the_upstream_write_finishes(
    tmp_path: Path,
):
    repository = Repository(Database(tmp_path / "reader.db"))
    repository.initialize()
    freshrss = BlockingFreshRSS()
    queue = DurableMutationQueue(repository, freshrss, retry_seconds=0.01)

    try:
        queue.submit("entry-1", state_kind="read", enabled=True)

        assert freshrss.send_started.wait(timeout=1)
        assert freshrss.local_states == [("read", ("entry-1",), True)]
        pending = repository.list_pending_mutations()
        assert [(item.entry_id, item.state_kind, item.enabled) for item in pending] == [
            ("entry-1", "read", True)
        ]

        freshrss.release_send.set()
        _wait_until(lambda: not repository.list_pending_mutations())
        assert freshrss.confirmed_states == [("read", ("entry-1",), True)]
    finally:
        freshrss.release_send.set()
        queue.close()


def test_newer_state_survives_an_older_in_flight_write(tmp_path: Path):
    repository = Repository(Database(tmp_path / "reader.db"))
    repository.initialize()
    freshrss = BlockingFreshRSS()
    queue = DurableMutationQueue(repository, freshrss, retry_seconds=0.01)

    try:
        queue.submit("entry-1", state_kind="read", enabled=True)
        assert freshrss.send_started.wait(timeout=1)

        queue.submit("entry-1", state_kind="read", enabled=False)
        freshrss.release_send.set()

        _wait_until(
            lambda: len(freshrss.sent_states) == 2
            and not repository.list_pending_mutations()
        )
        assert freshrss.sent_states == [
            ("read", ("entry-1",), True),
            ("read", ("entry-1",), False),
        ]
        assert freshrss.local_states[-1] == ("read", ("entry-1",), False)
        assert freshrss.confirmed_states == [("read", ("entry-1",), False)]
    finally:
        freshrss.release_send.set()
        queue.close()


def test_pending_mutation_resumes_after_process_restart(tmp_path: Path):
    database_path = tmp_path / "reader.db"
    first_repository = Repository(Database(database_path))
    first_repository.initialize()
    first_repository.queue_mutation(
        "entry-1",
        state_kind="starred",
        enabled=True,
    )

    restarted_repository = Repository(Database(database_path))
    restarted_repository.initialize()
    freshrss = BlockingFreshRSS()
    freshrss.release_send.set()
    queue = DurableMutationQueue(
        restarted_repository,
        freshrss,
        retry_seconds=0.01,
    )

    try:
        _wait_until(lambda: not restarted_repository.list_pending_mutations())
        assert freshrss.sent_states == [
            ("starred", ("entry-1",), True),
        ]
        assert freshrss.local_states == [("starred", ("entry-1",), True)]
        assert freshrss.confirmed_states == [("starred", ("entry-1",), True)]
    finally:
        queue.close()


def test_two_queues_serialize_flushes_and_preserve_the_newest_state(
    tmp_path: Path,
):
    database_path = tmp_path / "reader.db"
    first_repository = Repository(Database(database_path))
    first_repository.initialize()
    second_repository = Repository(Database(database_path))
    second_repository.initialize()
    first_repository.queue_mutation(
        "entry-1",
        state_kind="read",
        enabled=True,
    )
    upstream = ConcurrentBlockingFreshRSS()
    first_freshrss = FreshRSSProcessClient(upstream)
    second_freshrss = FreshRSSProcessClient(upstream)
    first_queue = DurableMutationQueue(
        first_repository,
        first_freshrss,
        retry_seconds=0.01,
        flush_poll_seconds=0.01,
        close_timeout_seconds=0.1,
    )
    second_queue = DurableMutationQueue(
        second_repository,
        second_freshrss,
        retry_seconds=0.01,
        flush_poll_seconds=0.01,
        close_timeout_seconds=0.1,
    )

    try:
        assert upstream.send_started.wait(timeout=1)
        second_queue.submit("entry-1", state_kind="read", enabled=False)

        assert not upstream.second_send_started.wait(timeout=0.15)
        assert upstream.maximum_active_sends == 1

        upstream.release_send.set()
        _wait_until(
            lambda: len(upstream.sent_states) == 2
            and not first_repository.list_pending_mutations()
            and first_freshrss.confirmed_states
            and second_freshrss.confirmed_states
        )

        assert upstream.sent_states == [
            ("read", ("entry-1",), True),
            ("read", ("entry-1",), False),
        ]
        assert upstream.maximum_active_sends == 1
        assert first_freshrss.local_states[-1] == ("read", ("entry-1",), False)
        assert second_freshrss.local_states[-1] == ("read", ("entry-1",), False)
        assert first_freshrss.confirmed_states == [
            ("read", ("entry-1",), False)
        ]
        assert second_freshrss.confirmed_states == [
            ("read", ("entry-1",), False)
        ]
    finally:
        upstream.release_send.set()
        first_queue.close()
        second_queue.close()


def test_non_owner_distinguishes_a_deleted_and_reinserted_version(
    tmp_path: Path,
):
    database_path = tmp_path / "reader.db"
    owner_repository = Repository(Database(database_path))
    owner_repository.initialize()
    owner_repository.queue_mutation(
        "entry-1",
        state_kind="starred",
        enabled=True,
    )
    upstream = ConcurrentBlockingFreshRSS()
    owner_freshrss = FreshRSSProcessClient(upstream)
    owner_queue = DurableMutationQueue(
        owner_repository,
        owner_freshrss,
        retry_seconds=0.01,
        flush_poll_seconds=0.01,
    )
    assert upstream.send_started.wait(timeout=1)

    non_owner_repository = PausingReconcileRepository(Database(database_path))
    non_owner_freshrss = FreshRSSProcessClient(upstream)
    non_owner_queue = DurableMutationQueue(
        non_owner_repository,
        non_owner_freshrss,
        retry_seconds=0.01,
        flush_poll_seconds=0.01,
    )

    try:
        assert non_owner_repository.reconcile_started.wait(timeout=1)

        upstream.release_send.set()
        _wait_until(lambda: not owner_repository.list_pending_mutations())
        upstream.release_send.clear()

        owner_queue.submit("entry-1", state_kind="starred", enabled=False)
        assert upstream.second_send_started.wait(timeout=1)

        pending = owner_repository.list_pending_mutations()
        assert [(item.version, item.enabled) for item in pending] == [(1, False)]

        non_owner_repository.release_reconcile.set()
        _wait_until(
            lambda: non_owner_freshrss.local_states[-1]
            == ("starred", ("entry-1",), False)
        )

        upstream.release_send.set()
        _wait_until(
            lambda: not owner_repository.list_pending_mutations()
            and bool(non_owner_freshrss.confirmed_states)
            and non_owner_freshrss.confirmed_states[-1]
            == ("starred", ("entry-1",), False)
        )
        assert owner_freshrss.confirmed_states[-1] == (
            "starred",
            ("entry-1",),
            False,
        )
    finally:
        upstream.release_send.set()
        non_owner_repository.release_reconcile.set()
        owner_queue.close()
        non_owner_queue.close()


def test_idle_non_owner_observes_and_confirms_an_owner_submission(
    tmp_path: Path,
):
    database_path = tmp_path / "reader.db"
    owner_repository = OwnerObservedRepository(Database(database_path))
    owner_repository.initialize()
    upstream = ConcurrentBlockingFreshRSS()
    owner_freshrss = FreshRSSProcessClient(upstream)
    owner_queue = DurableMutationQueue(
        owner_repository,
        owner_freshrss,
        retry_seconds=0.01,
        flush_poll_seconds=0.01,
    )
    assert owner_repository.worker_listed.wait(timeout=1)

    non_owner_repository = PausingReconcileRepository(Database(database_path))
    non_owner_freshrss = FreshRSSProcessClient(upstream)
    non_owner_queue = DurableMutationQueue(
        non_owner_repository,
        non_owner_freshrss,
        retry_seconds=0.01,
        flush_poll_seconds=0.01,
    )

    try:
        assert non_owner_repository.reconcile_started.wait(timeout=1)

        owner_queue.submit("entry-1", state_kind="starred", enabled=True)
        assert upstream.send_started.wait(timeout=1)

        non_owner_repository.release_reconcile.set()
        _wait_until(
            lambda: non_owner_freshrss.local_states
            == [("starred", ("entry-1",), True)]
        )

        upstream.release_send.set()
        _wait_until(
            lambda: not owner_repository.list_pending_mutations()
            and non_owner_freshrss.confirmed_states
            == [("starred", ("entry-1",), True)]
        )
    finally:
        upstream.release_send.set()
        non_owner_repository.release_reconcile.set()
        owner_queue.close()
        non_owner_queue.close()


def test_concurrent_toggle_cannot_apply_stale_state_after_confirmation(
    tmp_path: Path,
):
    repository = Repository(Database(tmp_path / "reader.db"))
    repository.initialize()
    freshrss = BlockingApplyFreshRSS()
    queue = DurableMutationQueue(
        repository,
        freshrss,
        retry_seconds=0.01,
        flush_poll_seconds=0.01,
    )
    submit_errors: list[BaseException] = []

    def submit(enabled: bool) -> None:
        try:
            queue.submit("entry-1", state_kind="starred", enabled=enabled)
        except BaseException as exc:  # pragma: no cover - asserted below
            submit_errors.append(exc)

    first_submit = threading.Thread(target=submit, args=(True,))
    second_submit: threading.Thread | None = None
    first_submit.start()
    try:
        assert freshrss.apply_started.wait(timeout=1)
        second_submit = threading.Thread(target=submit, args=(False,))
        second_submit.start()
        assert not freshrss.send_started.wait(timeout=0.15)

        freshrss.release_apply.set()
        first_submit.join(timeout=1)
        second_submit.join(timeout=1)
        assert not first_submit.is_alive()
        assert not second_submit.is_alive()
        assert submit_errors == []

        _wait_until(lambda: not repository.list_pending_mutations())
        assert freshrss.state[("starred", "entry-1")] == (False, "confirmed")
        assert freshrss.local_states[-1] == ("starred", ("entry-1",), False)
        assert freshrss.sent_states[-1] == ("starred", ("entry-1",), False)
        assert freshrss.confirmed_states[-1] == (
            "starred",
            ("entry-1",),
            False,
        )
    finally:
        freshrss.release_apply.set()
        first_submit.join(timeout=1)
        if second_submit is not None:
            second_submit.join(timeout=1)
        queue.close()


def test_new_submissions_do_not_bypass_retry_backoff(tmp_path: Path):
    repository = Repository(Database(tmp_path / "reader.db"))
    repository.initialize()
    freshrss = AlwaysFailingFreshRSS()
    retry_seconds = 0.15
    queue = DurableMutationQueue(
        repository,
        freshrss,
        retry_seconds=retry_seconds,
        flush_poll_seconds=0.01,
        close_timeout_seconds=0.05,
    )

    try:
        queue.submit("entry-1", state_kind="read", enabled=True)
        assert freshrss.wait_for_attempts(1, timeout=1)

        queue.submit("entry-2", state_kind="starred", enabled=True)
        queue.submit("entry-3", state_kind="read", enabled=True)

        assert freshrss.wait_for_attempts(2, timeout=1)
        assert freshrss.attempt_times[1] - freshrss.attempt_times[0] >= 0.12
        assert repository.list_pending_mutations()
    finally:
        queue.close()


def test_repository_failures_do_not_kill_the_worker(tmp_path: Path):
    repository = FaultInjectingRepository(Database(tmp_path / "reader.db"))
    repository.initialize()
    freshrss = BlockingFreshRSS()
    freshrss.release_send.set()
    queue = DurableMutationQueue(
        repository,
        freshrss,
        retry_seconds=0.01,
        flush_poll_seconds=0.01,
    )

    try:
        repository.fail_next_list = True
        repository.fail_next_acknowledge = True
        queue.submit("entry-1", state_kind="read", enabled=True)

        _wait_until(lambda: not repository.list_pending_mutations())

        assert freshrss.sent_states == [
            ("read", ("entry-1",), True),
            ("read", ("entry-1",), True),
        ]
        assert freshrss.confirmed_states == [("read", ("entry-1",), True)]
    finally:
        queue.close()


def test_close_has_a_deadline_and_leaves_an_in_flight_row_for_restart(
    tmp_path: Path,
):
    database_path = tmp_path / "reader.db"
    repository = Repository(Database(database_path))
    repository.initialize()
    blocked_freshrss = BlockingFreshRSS()
    queue = DurableMutationQueue(
        repository,
        blocked_freshrss,
        retry_seconds=0.01,
        close_timeout_seconds=0.05,
    )

    queue.submit("entry-1", state_kind="read", enabled=True)
    assert blocked_freshrss.send_started.wait(timeout=1)

    started_at = time.monotonic()
    queue.close()
    close_duration = time.monotonic() - started_at

    assert close_duration < 0.25
    assert [item.entry_id for item in repository.list_pending_mutations()] == [
        "entry-1"
    ]

    blocked_freshrss.release_send.set()

    restarted_freshrss = BlockingFreshRSS()
    restarted_freshrss.release_send.set()
    restarted_queue = DurableMutationQueue(
        Repository(Database(database_path)),
        restarted_freshrss,
        retry_seconds=0.01,
    )
    try:
        _wait_until(lambda: not repository.list_pending_mutations())
        assert restarted_freshrss.local_states == [
            ("read", ("entry-1",), True)
        ]
        assert restarted_freshrss.sent_states == [
            ("read", ("entry-1",), True)
        ]
    finally:
        restarted_queue.close()


def test_transient_upstream_failure_retries_without_losing_the_action(
    tmp_path: Path,
):
    repository = Repository(Database(tmp_path / "reader.db"))
    repository.initialize()
    freshrss = FlakyFreshRSS()
    queue = DurableMutationQueue(repository, freshrss, retry_seconds=0.01)

    try:
        queue.submit("entry-1", state_kind="read", enabled=True)
        _wait_until(lambda: not repository.list_pending_mutations())

        assert freshrss.attempts == 2
        assert freshrss.sent_states == [("read", ("entry-1",), True)]
    finally:
        queue.close()
