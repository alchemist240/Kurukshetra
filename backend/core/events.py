import asyncio
from datetime import datetime, timezone
from typing import Any


class EventBus:
    """Lightweight Pub/Sub bus. Streams live events to the UI now and Phase 2 agents later."""

    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    async def publish(self, event_type: str, data: dict[str, Any]) -> None:
        event = {
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        for queue in list(self._subscribers):
            await queue.put(event)