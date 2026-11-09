"""按 key 的 asyncio 锁，用完即释放条目。

``dict[str, asyncio.Lock]`` + ``setdefault`` 的写法每个会话留一把锁、永不删除，
长期运行会随会话数单调增长。这里按引用计数管理：最后一个持有者 / 等待者离开时
把条目删掉，字典大小只跟「此刻正在用锁的 key 数」有关。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class KeyedLocks:
    def __init__(self) -> None:
        self._entries: dict[str, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        lock, refs = self._entries.get(key, (None, 0))
        if lock is None:
            lock = asyncio.Lock()
        self._entries[key] = (lock, refs + 1)
        try:
            async with lock:
                yield
        finally:
            lock_now, refs_now = self._entries[key]
            if refs_now <= 1:
                del self._entries[key]
            else:
                self._entries[key] = (lock_now, refs_now - 1)

    def __len__(self) -> int:
        return len(self._entries)
