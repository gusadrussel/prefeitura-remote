"""Bounded shutdown of tasks owned by a WebSocket handler."""
import asyncio
import logging

import anyio

LOGGER = logging.getLogger("prefeitura_remote.tasks")


async def cancel_tasks(*tasks: asyncio.Task | None) -> None:
    owned = list(dict.fromkeys(task for task in tasks if task is not None and task is not asyncio.current_task()))
    def observe(task):
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                LOGGER.debug('Owned task ended during shutdown: %s', type(error).__name__)
    for task in owned:
        # Observe even if a second cancellation interrupts this caller's wait.
        task.add_done_callback(observe)
        task.cancel()
    if not owned:
        return
    # Shield AnyIO scope cancellation, but preserve direct Task.cancel(). Registry
    # cleanup must already have run before this cancellation checkpoint.
    with anyio.move_on_after(2, shield=True):
        done, pending = await asyncio.wait(owned, timeout=1.5)
        for task in done:
            if not task.cancelled():
                error = task.exception()
                if error is not None and not isinstance(error, (RuntimeError, ValueError)):
                    LOGGER.debug("Task ended during disconnect: %s", type(error).__name__)
        if pending:
            LOGGER.warning("%d WebSocket tasks did not stop within shutdown deadline", len(pending))
            for task in pending:
                task.cancel()
