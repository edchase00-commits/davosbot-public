"""Bound repeated limit notices without changing who may execute a request."""

from collections import OrderedDict
import threading
import time


class NoticeThrottle:
    def __init__(self, cooldown=300, max_keys=512, clock=time.monotonic):
        self.cooldown = cooldown
        self.max_keys = max_keys
        self.clock = clock
        self._until = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, sender: str, recipient: str) -> bool:
        """Reserve one notification attempt for this sender and destination."""
        key = (sender, recipient)
        with self._lock:
            now = self.clock()
            while self._until and next(iter(self._until.values())) <= now:
                self._until.popitem(last=False)
            if self._until.get(key, 0) > now:
                return False
            if len(self._until) >= self.max_keys:
                self._until.popitem(last=False)
            self._until[key] = now + self.cooldown
            return True


allow = NoticeThrottle().allow
