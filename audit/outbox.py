"""Deliver persisted audit events while the API is running."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from domain.repository import SupportRepository


logger = logging.getLogger(__name__)
_POLL_INTERVAL_SECONDS = 1.0


async def _publish_events(repository: SupportRepository, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            if await repository.publish_audit_events():
                continue
        except Exception:
            logger.exception("审计 Outbox 投递失败，稍后重试")

        try:
            await asyncio.wait_for(stop.wait(), timeout=_POLL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def audit_outbox_worker(repository: SupportRepository) -> AsyncIterator[None]:
    stop = asyncio.Event()
    task = asyncio.create_task(_publish_events(repository, stop), name="audit-outbox")
    try:
        yield
    finally:
        # Wait for the current SQLite transaction; the next startup resumes pending events.
        stop.set()
        await task
