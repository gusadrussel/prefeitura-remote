"""Bounded thread-safe mailboxes with cancellation-safe asynchronous consumers.

HTTP worker threads never touch asyncio Futures directly: notifications are
scheduled on each consumer's event loop. Items stay queued until consumed, so
cancelling a waiting consumer cannot lose a command.
"""
import asyncio
from collections import deque
from functools import wraps
from threading import RLock


def synchronized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


class _Notifications:
    def __init__(self):
        self._lock = RLock()
        self._waiters = set()

    def _notify(self):
        waiters, self._waiters = self._waiters, set()
        for loop, future in waiters:
            def wake(target=future):
                if not target.done():
                    target.set_result(None)
            try:
                loop.call_soon_threadsafe(wake)
            except RuntimeError:
                # A closed consumer loop has no active waiter to wake.
                if not loop.is_closed():
                    raise


class Mailbox(_Notifications):
    def __init__(self, maxsize: int):
        if maxsize < 1:
            raise ValueError("Mailbox must have a positive capacity")
        super().__init__()
        self.maxsize = maxsize
        self._items = deque()

    @synchronized
    def qsize(self):
        return len(self._items)

    def empty(self):
        return self.qsize() == 0

    def full(self):
        return self.qsize() >= self.maxsize

    @synchronized
    def put_nowait(self, item):
        if len(self._items) >= self.maxsize:
            raise asyncio.QueueFull
        self._items.append(item)
        self._notify()

    @synchronized
    def get_nowait(self):
        if not self._items:
            raise asyncio.QueueEmpty
        return self._items.popleft()

    @synchronized
    def replace(self, item):
        """Atomically discard pending commands and enqueue an urgent stop."""
        self._items.clear()
        self._items.append(item)
        self._notify()

    @synchronized
    def put_latest(self, item):
        dropped = len(self._items) >= self.maxsize
        if dropped:
            self._items.popleft()
        self._items.append(item)
        self._notify()
        return dropped

    @synchronized
    def clear(self):
        self._items.clear()

    async def get(self):
        loop = asyncio.get_running_loop()
        while True:
            with self._lock:
                if self._items:
                    return self._items.popleft()
                future = loop.create_future()
                waiter = (loop, future)
                self._waiters.add(waiter)
            try:
                await future
            finally:
                with self._lock:
                    self._waiters.discard(waiter)


class Signal(_Notifications):
    def __init__(self):
        super().__init__()
        self._flag = False

    @synchronized
    def is_set(self):
        return self._flag

    @synchronized
    def set(self):
        self._flag = True
        self._notify()

    @synchronized
    def clear(self):
        self._flag = False

    async def wait(self):
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._flag:
                return True
            future = loop.create_future()
            waiter = (loop, future)
            self._waiters.add(waiter)
        try:
            await future
            return True
        finally:
            with self._lock:
                self._waiters.discard(waiter)
