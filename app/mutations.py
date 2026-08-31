from __future__ import annotations

import errno
import fcntl
import logging
import os
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Literal, Protocol

from app.repository import PendingMutation, Repository

MutationKind = Literal["read", "starred"]

logger = logging.getLogger(__name__)

MAX_RETRY_SECONDS = 30.0
DEFAULT_FLUSH_POLL_SECONDS = 0.5
DEFAULT_CLOSE_TIMEOUT_SECONDS = 1.0


class MutableFreshRSS(Protocol):
    def mark_read(self, entry_ids: Iterable[str]) -> None: ...

    def mark_unread(self, entry_ids: Iterable[str]) -> None: ...

    def mark_starred(self, entry_ids: Iterable[str]) -> None: ...

    def mark_unstarred(self, entry_ids: Iterable[str]) -> None: ...


class MutationService(Protocol):
    def submit(self, entry_id: str, *, state_kind: MutationKind, enabled: bool) -> None: ...


class ImmediateMutationService:
    """Use synchronous writes for injected services and tests."""

    def __init__(self, freshrss: MutableFreshRSS):
        self.freshrss = freshrss

    def submit(
        self,
        entry_id: str,
        *,
        state_kind: MutationKind,
        enabled: bool,
    ) -> None:
        _mutation_operation(self.freshrss, state_kind, enabled)([entry_id])

    def close(self) -> None:
        return None


class DurableMutationQueue:
    """Commit reader actions locally, then batch FreshRSS writes off the request path."""

    def __init__(
        self,
        repository: Repository,
        freshrss: MutableFreshRSS,
        *,
        batch_size: int = 50,
        retry_seconds: float = 1.0,
        flush_poll_seconds: float = DEFAULT_FLUSH_POLL_SECONDS,
        close_timeout_seconds: float = DEFAULT_CLOSE_TIMEOUT_SECONDS,
    ):
        self.repository = repository
        self.freshrss = freshrss
        self.batch_size = max(1, batch_size)
        self.retry_seconds = max(0.01, retry_seconds)
        self.flush_poll_seconds = max(0.01, flush_poll_seconds)
        self.close_timeout_seconds = max(0.0, close_timeout_seconds)
        self._local_state_lock = threading.RLock()
        self._tracked_local_mutations: dict[
            tuple[str, str], PendingMutation
        ] = {}
        self._condition = threading.Condition()
        self._closed = False
        self._generation = 0
        self._pending_state_restored = self._restore_pending_local_state()
        database_path = self.repository.database.path.resolve()
        self._flush_lock_path = database_path.with_name(
            f"{database_path.name}.mutations.lock"
        )
        self._thread = threading.Thread(
            target=self._run,
            name="rss-kindle-mutations",
            daemon=True,
        )
        self._thread.start()

    def submit(
        self,
        entry_id: str,
        *,
        state_kind: MutationKind,
        enabled: bool,
    ) -> None:
        queued = False
        try:
            with self._local_state_lock:
                mutation = self.repository.queue_mutation(
                    entry_id,
                    state_kind=state_kind,
                    enabled=enabled,
                )
                queued = True
                self._apply_pending_local_state([mutation])
        finally:
            if queued:
                with self._condition:
                    self._generation += 1
                    self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._thread.join(timeout=self.close_timeout_seconds)
        if self._thread.is_alive():
            logger.warning(
                "FreshRSS mutation worker did not stop before the shutdown deadline; "
                "queued rows remain durable"
            )

    def _run(self) -> None:
        retry_count = 0
        flush_lock = _FlushFileLock(self._flush_lock_path)
        try:
            while True:
                with self._condition:
                    if self._closed:
                        return
                    observed_generation = self._generation

                if not flush_lock.acquired:
                    try:
                        acquired = flush_lock.try_acquire()
                    except OSError:
                        retry_count = min(retry_count + 1, 6)
                        logger.warning(
                            "Could not acquire the FreshRSS mutation flush lock",
                            exc_info=True,
                        )
                        self._wait_for_retry(retry_count)
                        continue
                    if not acquired:
                        if not self._reconcile_non_owner_state():
                            retry_count = min(retry_count + 1, 6)
                            self._wait_for_retry(retry_count)
                            continue
                        retry_count = 0
                        self._wait_for_work(observed_generation)
                        continue

                try:
                    with self._local_state_lock:
                        mutations = self.repository.list_pending_mutations(
                            limit=self.batch_size * 4
                        )
                        if not self._pending_state_restored:
                            self._pending_state_restored = (
                                self._restore_pending_local_state(mutations)
                            )
                        else:
                            self._apply_pending_local_state(mutations)
                        if len(mutations) < self.batch_size * 4:
                            self._confirm_missing_local_mutations(mutations)
                except Exception:  # noqa: BLE001 - keep the durable worker alive
                    retry_count = min(retry_count + 1, 6)
                    logger.warning(
                        "Could not read queued FreshRSS mutations", exc_info=True
                    )
                    self._wait_for_retry(retry_count)
                    continue

                batch = _next_batch(mutations, self.batch_size)
                if not batch:
                    retry_count = 0
                    self._wait_for_work(observed_generation)
                    continue

                with self._condition:
                    if self._closed:
                        return

                first = batch[0]
                entry_ids = [mutation.entry_id for mutation in batch]
                try:
                    self._send_state(
                        first.state_kind,
                        entry_ids,
                        enabled=first.enabled,
                    )
                    with self._condition:
                        if self._closed:
                            return
                    acknowledged_ids = self.repository.acknowledge_mutations(batch)
                except Exception:  # noqa: BLE001 - durable rows remain queued for retry
                    retry_count = min(retry_count + 1, 6)
                    logger.warning("FreshRSS mutation flush failed", exc_info=True)
                    self._wait_for_retry(retry_count)
                    continue

                self._confirm_acknowledged_mutations(batch, acknowledged_ids)
                retry_count = 0
        finally:
            flush_lock.close()

    def _wait_for_retry(self, retry_count: int) -> None:
        retry_deadline = time.monotonic() + min(
            self.retry_seconds * (2 ** (retry_count - 1)),
            MAX_RETRY_SECONDS,
        )
        with self._condition:
            while not self._closed:
                remaining = retry_deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._condition.wait(timeout=remaining)

    def _wait_for_work(self, observed_generation: int) -> None:
        with self._condition:
            self._condition.wait_for(
                lambda: self._closed or self._generation != observed_generation,
                timeout=self.flush_poll_seconds,
            )

    def _restore_pending_local_state(
        self, mutations: list[PendingMutation] | None = None
    ) -> bool:
        try:
            with self._local_state_lock:
                pending = (
                    mutations
                    if mutations is not None
                    else self.repository.list_pending_mutations(limit=None)
                )
                self._apply_pending_local_state(pending)
            return True
        except Exception:  # noqa: BLE001 - remote durability must survive cache failures
            logger.warning(
                "Could not restore queued FreshRSS state into local caches",
                exc_info=True,
            )
            return False

    def _apply_pending_local_state(
        self,
        mutations: list[PendingMutation],
    ) -> None:
        pending_updates = [
            mutation
            for mutation in mutations
            if self._tracked_local_mutations.get(
                (mutation.state_kind, mutation.entry_id)
            )
            != mutation
        ]
        if not pending_updates:
            return
        grouped: dict[tuple[str, bool], list[PendingMutation]] = {}
        for mutation in pending_updates:
            grouped.setdefault(
                (mutation.state_kind, mutation.enabled), []
            ).append(mutation)
        apply_local_state = getattr(self.freshrss, "apply_local_state", None)
        for (state_kind, enabled), grouped_mutations in grouped.items():
            if callable(apply_local_state):
                apply_local_state(
                    state_kind,
                    [mutation.entry_id for mutation in grouped_mutations],
                    enabled=enabled,
                )
            for mutation in grouped_mutations:
                self._tracked_local_mutations[
                    (mutation.state_kind, mutation.entry_id)
                ] = mutation

    def _reconcile_non_owner_state(self) -> bool:
        with self._local_state_lock:
            try:
                pending = self.repository.list_pending_mutations(limit=None)
                self._apply_pending_local_state(pending)
                self._confirm_missing_local_mutations(pending)
                return True
            except Exception:  # noqa: BLE001 - retry cache reconciliation later
                logger.warning(
                    "Could not reconcile queued FreshRSS state in this process",
                    exc_info=True,
                )
                return False

    def _confirm_missing_local_mutations(
        self,
        pending: list[PendingMutation],
    ) -> None:
        pending_keys = {
            (mutation.state_kind, mutation.entry_id) for mutation in pending
        }
        completed = [
            mutation
            for key, mutation in self._tracked_local_mutations.items()
            if key not in pending_keys
        ]
        self._confirm_tracked_mutations(completed)

    def _confirm_acknowledged_mutations(
        self,
        batch: list[PendingMutation],
        acknowledged_ids: list[str],
    ) -> None:
        if not acknowledged_ids:
            return
        acknowledged = set(acknowledged_ids)
        with self._local_state_lock:
            confirmed = []
            for mutation in batch:
                if mutation.entry_id not in acknowledged:
                    continue
                tracked = self._tracked_local_mutations.get(
                    (mutation.state_kind, mutation.entry_id)
                )
                if tracked is None or tracked == mutation:
                    confirmed.append(mutation)
            self._confirm_tracked_mutations(confirmed)

    def _confirm_tracked_mutations(
        self,
        mutations: list[PendingMutation],
    ) -> None:
        grouped: dict[tuple[str, bool], list[PendingMutation]] = {}
        for mutation in mutations:
            grouped.setdefault(
                (mutation.state_kind, mutation.enabled), []
            ).append(mutation)
        for (state_kind, enabled), grouped_mutations in grouped.items():
            self._confirm_local_state(
                state_kind,
                [mutation.entry_id for mutation in grouped_mutations],
                enabled=enabled,
            )
            for mutation in grouped_mutations:
                key = (mutation.state_kind, mutation.entry_id)
                tracked = self._tracked_local_mutations.get(key)
                if tracked == mutation:
                    self._tracked_local_mutations.pop(key, None)

    def _confirm_local_state(
        self,
        state_kind: str,
        entry_ids: list[str],
        *,
        enabled: bool,
    ) -> None:
        confirm_local_state = getattr(self.freshrss, "confirm_local_state", None)
        if not callable(confirm_local_state):
            return
        try:
            confirm_local_state(state_kind, entry_ids, enabled=enabled)
        except Exception:  # noqa: BLE001 - the durable upstream write already succeeded
            logger.warning(
                "Could not confirm flushed FreshRSS state in local caches",
                exc_info=True,
            )

    def _send_state(
        self,
        state_kind: str,
        entry_ids: list[str],
        *,
        enabled: bool,
    ) -> None:
        send_state = getattr(self.freshrss, "send_state", None)
        if callable(send_state):
            send_state(state_kind, entry_ids, enabled=enabled)
            return
        _mutation_operation(self.freshrss, state_kind, enabled)(entry_ids)


class _FlushFileLock:
    """Hold one advisory flush lock for all processes that share a database."""

    def __init__(self, path: Path):
        self.path = path
        self._file_descriptor: int | None = None
        self.acquired = False

    def try_acquire(self) -> bool:
        if self.acquired:
            return True
        if self._file_descriptor is None:
            flags = os.O_CREAT | os.O_RDWR
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            self._file_descriptor = os.open(self.path, flags, 0o600)
        try:
            fcntl.flock(
                self._file_descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise
        self.acquired = True
        return True

    def close(self) -> None:
        file_descriptor = self._file_descriptor
        if file_descriptor is None:
            return
        try:
            if self.acquired:
                fcntl.flock(file_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(file_descriptor)
            self._file_descriptor = None
            self.acquired = False


def _next_batch(
    mutations: list[PendingMutation], batch_size: int
) -> list[PendingMutation]:
    if not mutations:
        return []
    first = mutations[0]
    return [
        mutation
        for mutation in mutations
        if mutation.state_kind == first.state_kind
        and mutation.enabled == first.enabled
    ][:batch_size]


def _mutation_operation(
    freshrss: MutableFreshRSS,
    state_kind: str,
    enabled: bool,
):
    if state_kind == "read":
        return freshrss.mark_read if enabled else freshrss.mark_unread
    if state_kind == "starred":
        return freshrss.mark_starred if enabled else freshrss.mark_unstarred
    raise ValueError(f"Unsupported mutation state: {state_kind}")
