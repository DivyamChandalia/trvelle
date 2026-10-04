"""Planning tasks outlive browser connections; subscribers can reconnect."""
import asyncio
import time
from contextlib import suppress


class ChatRun:
    def __init__(self, source):
        self.events = []
        self.changed = asyncio.Event()
        self.failed = False
        self.finished_at = None
        self.task = asyncio.create_task(self._produce(source))

    async def _produce(self, source):
        try:
            async for event in source:
                self.events.append(event)
                if event.startswith('event: error\n'):
                    self.failed = True
                self.changed.set()
        except asyncio.CancelledError:
            self.failed = True
            raise
        except Exception:
            self.failed = True
            self.events.append('event: error\ndata: "Planning stopped. Use Resume to continue from saved research."\n\n')
        finally:
            self.finished_at = time.monotonic()
            self.changed.set()

    async def subscribe(self, after=0):
        # Closing a subscriber never cancels the planning task.
        index = after
        while True:
            while index < len(self.events):
                yield self.events[index]
                index += 1
            if self.finished_at is not None:
                return
            self.changed.clear()
            await self.changed.wait()


class ChatRuns:
    def __init__(self):
        self.runs = {}

    def get(self, owner, chat):
        return self.runs.get((owner, chat))

    def start(self, owner, chat, source):
        now = time.monotonic()
        for key, run in list(self.runs.items()):
            if run.finished_at is not None and now - run.finished_at > 3600:
                del self.runs[key]
        run = ChatRun(source)
        self.runs[(owner, chat)] = run
        return run

    async def stop(self, owner, chat):
        run = self.runs.pop((owner, chat), None)
        if run:
            run.task.cancel()
            with suppress(asyncio.CancelledError):
                await run.task

    async def close(self):
        for run in self.runs.values():
            if not run.task.done():
                run.task.cancel()
        for run in self.runs.values():
            with suppress(asyncio.CancelledError):
                await run.task
        self.runs.clear()
