"""Bounded handler workers; durable inbox claims supply chat/sender reservations."""

import threading
import time
from contextlib import contextmanager

from .inbox_ownership import acquire_inbox, release_inbox

WORKER_COUNT = 3


class InboxWorkerError(RuntimeError):
    pass


class InboxWorkers:
    def __init__(self, inbox, handler):
        self.inbox = inbox
        self.handler = handler
        self._condition = threading.Condition()
        self._stopping = False
        self._started = False
        self._generation = 0
        self._live = 0
        self._failure = None
        self._threads = []

    def start(self):
        with self._condition:
            if self._started:
                raise RuntimeError("inbox_workers_already_started")
            acquire_inbox(self.inbox.db_path, self)
            self._started = True
            try:
                for number in range(WORKER_COUNT):
                    thread = threading.Thread(target=self._run, name=f"inbox-handler-{number + 1}", daemon=True)
                    thread.start()
                    self._threads.append(thread)
                    self._live += 1
            except BaseException:
                self._stopping = True
                self._condition.notify_all()
                if not self._live:
                    release_inbox(self.inbox.db_path, self)
                raise

    def wake(self):
        """Coalesced signal only: message bodies never enter an in-memory queue."""
        with self._condition:
            self._generation += 1
            self._condition.notify_all()

    @contextmanager
    def _admission(self):
        with self._condition:
            yield not self._stopping

    def _fail(self, error):
        with self._condition:
            self._failure = self._failure or type(error).__name__
            self._stopping = True
            self._condition.notify_all()

    def _handle(self, message):
        try:
            return self.handler(message)
        except BaseException as error:
            # Stop admission before dispatch_ready releases the processing
            # reservation as uncertain. A following confirmation must not slip
            # between that commit and propagation of this handler exception.
            self._fail(error)
            raise

    def _run(self):
        generation = 0
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._stopping or self._generation != generation)
                    if self._stopping:
                        return
                    generation = self._generation
                while True:
                    with self._condition:
                        if self._stopping:
                            return
                    # One claim is immediately executed in this thread. A stop
                    # does not cancel an in-flight claim or its handler.
                    if not self.inbox.dispatch_ready(self._handle, limit=1, admission=self._admission):
                        break
                    self.wake()  # Completing a reservation may unblock peers.
        except BaseException as error:
            # dispatch_ready preserves uncertain/processing claims. Do not
            # replace a failed worker or repeat its potentially external effect.
            self._fail(error)
        finally:
            with self._condition:
                self._live -= 1
                if not self._live:
                    release_inbox(self.inbox.db_path, self)
                self._condition.notify_all()

    def raise_if_failed(self):
        with self._condition:
            failure = self._failure
        if failure:
            raise InboxWorkerError(f"inbox_worker_stopped:{failure}")

    def stop(self, timeout=1.0):
        """Stop admission and wait a bounded total time; return live thread count.

        Running handlers retain ownership and their durable processing claims.
        They are never marked stopped or replayable merely because the join ends.
        """
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        with self._condition:
            return self._live
