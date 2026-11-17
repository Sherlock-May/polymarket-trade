import asyncio
import copy
import time
from types import SimpleNamespace as NS

import pytest

from bot.config import D
from bot.polymarket_client import LiveBroker, OrderUnavailableError
from bot.websocket_client import MarketFeed
from test_live_adapter import Pages, null_order_response


async def test_market_books_update_before_main_queue_consumes():
    queued = []
    ready = asyncio.Event()
    class Stream:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def __aiter__(self): return self.events()
        async def events(self):
            yield NS(type="book", payload={"asset_id": "up", "bids": [], "asks": []})
            ready.set()
            await asyncio.Event().wait()
    class Client:
        async def subscribe(self, spec): return Stream()
    async def emit(kind, data): queued.append((kind, data))
    feed = MarketFeed(Client(), emit, immediate=True)
    feed.specs = {"up": {"tick_size": "0.001", "min_order_size": "10"}}
    await feed.set_tokens({"up"})
    try:
        await asyncio.wait_for(ready.wait(), 1)
        assert feed.books["up"].tick == "0.001"
        assert feed.books["up"].min_size == "10"
        assert queued[-1][1]["applied"]
    finally:
        await feed.close()


@pytest.mark.parametrize("bad", [False, True])
async def test_idle_book_refreshed_before_live_entry(engine, bad):
    engine.mode = "live"
    engine.user_connected = engine.connection_reconciled = engine.geo_allowed = True
    for b in engine.feed.books.values(): b.received_at = 0
    calls = []
    async def book(token):
        calls.append(token)
        return {"asset_id": "wrong" if bad else token, "bids": [], "asks": [],
                "timestamp": time.time() * 1000, "tick_size": "0.01", "min_order_size": "5"}
    async def market(slug): return {"acceptingOrders": True, "closed": False}
    engine.api.book, engine.api.market = book, market
    await engine.open_round(engine.rounds["test-round"])
    assert bool(engine.orders) is (not bad)
    assert calls


async def test_old_round_queries_never_enter_active_reconcile(engine):
    r = engine.rounds["test-round"]
    await engine.submit(r, "up", "BUY", "0.10", "10", "ENTRY")
    engine.api.value = r["market"]["end"] + 120
    class Client:
        def list_open_orders(self, **kwargs): pytest.fail("old network call in active path")
    engine.broker = LiveBroker(engine, Client())
    await engine.reconcile()
    assert engine.connection_reconciled


async def test_old_cancel_never_blocks_current_manage(engine):
    old = copy.deepcopy(engine.rounds["test-round"])
    old["market"]["slug"] = "old"
    old["market"]["end"] = engine.api.now() - 120
    engine.rounds["old"] = old
    old_order = await engine.submit(engine.rounds["test-round"], "up", "BUY", "0.10", "10", "ENTRY")
    old_order.update(slug="old", side="SELL")
    engine.mode = "live"
    async def cancel(*args): pytest.fail("archive cancellation in main loop")
    seen = []
    async def open_round(r): seen.append(r["market"]["slug"])
    engine.cancel, engine.open_round = cancel, open_round
    await engine.manage()
    assert seen == ["test-round"]


@pytest.mark.parametrize("ack", [False, True])
async def test_null_detail_needs_explicit_cancel_ack(engine, ack):
    class Client:
        async def cancel_order(self, **kwargs): return NS(canceled=["server"] if ack else [])
        async def get_order(self, **kwargs): return null_order_response()
        def list_open_orders(self, **kwargs): return Pages([])
    o = {"exchange_id": "server", "status": "CANCEL_PENDING", "matched": "2"}
    broker = LiveBroker(engine, Client())
    if ack:
        assert await broker.cancel(o)
        assert o["matched"] == "2"
        assert o["trade_reconcile_required"]
    else:
        with pytest.raises(OrderUnavailableError): await broker.cancel(o)


@pytest.mark.parametrize("resolved", [False, True])
async def test_old_missing_exit_requires_resolved_market(engine, resolved):
    r = engine.rounds["test-round"]
    o = await engine.submit(r, "up", "SELL", "0.20", "10", "TAKE_PROFIT")
    o.update(exchange_id="server", status="CANCEL_PENDING")
    engine.api.value = r["market"]["end"] + 120
    async def market(slug): return {"closed": resolved, "umaResolutionStatus": "resolved" if resolved else "disputed"}
    engine.api.market = market
    class Client:
        async def get_order(self, **kwargs): return null_order_response()
        def list_open_orders(self, **kwargs): return Pages([])
        def list_account_trades(self, **kwargs): return Pages([])
    await LiveBroker(engine, Client()).reconcile(archived=True)
    assert o["status"] == ("CANCELED" if resolved else "CANCEL_PENDING")
    assert engine.fills == []


async def test_cancel_ack_cannot_finalize_before_trade_reconciliation(engine):
    r = engine.rounds["test-round"]
    o = await engine.submit(r, "up", "SELL", "0.20", "10", "TAKE_PROFIT")
    o.update(status="CANCELED", trade_reconcile_required=True)
    engine.api.value = r["market"]["end"] + 120
    await engine.settle()
    assert r["status"] != "FINAL"


async def test_archive_wait_does_not_hold_current_reconcile(engine):
    entered = asyncio.Event()
    class Broker(LiveBroker):
        async def reconcile(self, archived=None):
            if archived:
                entered.set()
                await asyncio.Event().wait()
            return []
    engine.mode = "live"
    engine.broker = Broker(engine, None)
    task = asyncio.create_task(engine.archive_worker())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(engine.reconcile(), 1)
        assert engine.connection_reconciled and not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
