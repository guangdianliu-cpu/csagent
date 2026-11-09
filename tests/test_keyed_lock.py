import asyncio

from csagent.common.keyed_lock import KeyedLocks


async def test_mutual_exclusion_and_cleanup():
    locks = KeyedLocks()
    order: list[str] = []

    async def worker(name: str, key: str, delay: float):
        async with locks.hold(key):
            order.append(f"{name}+")
            await asyncio.sleep(delay)
            order.append(f"{name}-")

    await asyncio.gather(worker("a", "k", 0.02), worker("b", "k", 0.0), worker("c", "other", 0.0))
    # 同 key 串行：a 完整结束后 b 才开始
    ai, bi = order.index("a-"), order.index("b+")
    assert ai < bi
    # 全部结束后不留条目
    assert len(locks) == 0


async def test_exception_releases_entry():
    locks = KeyedLocks()
    try:
        async with locks.hold("k"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert len(locks) == 0
    async with locks.hold("k"):
        pass
