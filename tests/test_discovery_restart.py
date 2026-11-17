import asyncio
from dataclasses import replace

import pytest

from bot.engine import Engine
from bot.market_discovery import PublicAPI
from test_models_config import sample


@pytest.mark.parametrize("block", [None, "order", "attempted", "started", "disabled", "other_reason", "final"])
async def test_resume_only_unattempted_future_stopped_round(engine, block):
    r = engine.rounds["test-round"]
    if block == "order":
        await engine.submit(r, "up", "BUY", "0.10", "10", "ENTRY")
    r.update(entry_closed=True, skip_reason="配置关闭或安全停机")
    if block == "attempted": r["entry_attempted"] = True
    if block == "started": engine.api.value = r["market"]["start"]
    if block == "disabled": engine.config.markets = [replace(engine.config.markets[0], enabled=False)]
    if block == "other_reason": r["skip_reason"] = "行情断线"
    if block == "final": r["status"] = "FINAL"
    engine.save()
    restarted = Engine(engine.db, engine.config, engine.api, engine.feed, engine.log)
    restarted.resume_waiting_rounds()
    restored = restarted.rounds["test-round"]
    assert restored["entry_closed"] is (block is not None)
    assert len(restarted.orders) == len(engine.orders)
    assert engine.db.rounds()[0]["entry_closed"] is (block is not None)


async def test_slow_market_does_not_delay_other_discovery_and_cancels_children(engine):
    slow, fast = asyncio.Event(), asyncio.Event()
    stopped = asyncio.Event()
    one = engine.config.markets[0]
    two = replace(one, id="eth-15m", symbol="ETH")
    engine.config.markets = [one, two]
    async def discover(rule):
        if rule == one:
            slow.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        fast.set()
        return engine.market("test-round")
    engine.api.discover = discover
    task = asyncio.create_task(engine.discover_loop())
    try:
        await asyncio.wait_for(slow.wait(), 1)
        await asyncio.wait_for(fast.wait(), 1)
        kind, (rule, market) = await asyncio.wait_for(engine.queue.get(), 1)
        assert kind == "discovered" and rule == two
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert stopped.is_set()


@pytest.mark.parametrize("valid", [False, True])
async def test_bad_market_row_does_not_hide_valid_future_market(engine, valid):
    api = PublicAPI()
    api.now = lambda: 1794816600
    rule = replace(engine.config.markets[0], symbol="BTC", timeframe="4h")
    async def get(url, **params):
        if url.endswith("/series"):
            return [{"id": "series-id", "slug": rule.series_slug}]
        rows = [{"slug": "malformed"}]
        if valid: rows.append(sample())
        return [{"startTime": "2026-11-16T09:00:00Z", "markets": rows}]
    api.get = get
    try:
        if valid:
            market = await api.discover(rule)
            assert market.start == 1794819600
        else:
            with pytest.raises(ValueError): await api.discover(rule)
    finally:
        await api.close()
