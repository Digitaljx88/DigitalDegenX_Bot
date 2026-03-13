from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict
from typing import Any

_chat_locks: dict[str, asyncio.Lock] = {}
_next_send_ts: dict[str, float] = defaultdict(float)


def _chat_key(chat_id: Any) -> str:
    return str(chat_id)


async def send_throttled_message(
    bot,
    *,
    chat_id,
    min_interval_secs: float = 1.5,
    retries: int = 2,
    **kwargs,
):
    """
    Serialize sends per chat and honor Telegram retry-after hints.
    This is intended for channel/group traffic where bursts trigger flood limits.
    """
    key = _chat_key(chat_id)
    if key not in _chat_locks:
        _chat_locks[key] = asyncio.Lock()

    async with _chat_locks[key]:
        for attempt in range(retries + 1):
            wait_for_slot = _next_send_ts[key] - time.monotonic()
            if wait_for_slot > 0:
                await asyncio.sleep(wait_for_slot)

            try:
                message = await bot.send_message(chat_id=chat_id, **kwargs)
                _next_send_ts[key] = time.monotonic() + max(0.0, min_interval_secs)
                return message
            except Exception as exc:
                retry_after = getattr(exc, "retry_after", None)
                if retry_after is None:
                    match = re.search(r"Retry in (\d+)", str(exc))
                    retry_after = int(match.group(1)) if match else None
                if retry_after is None or attempt >= retries:
                    raise
                _next_send_ts[key] = time.monotonic() + float(retry_after) + 1.0
                await asyncio.sleep(float(retry_after) + 1.0)
