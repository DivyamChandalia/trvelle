"""Keep SSE connections alive while a model or researcher is working."""
import asyncio
from contextlib import suppress


async def with_keepalive(source, interval=15):
    iterator = source.__aiter__()
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(anext(iterator))
            ready, _ = await asyncio.wait({pending}, timeout=interval)
            if not ready:
                yield ": keepalive\n\n"
                continue
            try:
                item = pending.result()
            except StopAsyncIteration:
                return
            finally:
                pending = None
            yield item
    finally:
        if pending is not None:
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending
        await iterator.aclose()
