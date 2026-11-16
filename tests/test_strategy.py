from dataclasses import asdict, replace
import time
import pytest
from bot.config import D
from bot.database import Database
from bot.engine import Engine
from bot.models import Fill, Market
from bot.polymarket_client import PaperBroker, ACTIVE


def round_(e):
    return e.rounds["test-round"]


async def buy(e, token="up", size="10", price="0.10"):
    r = round_(e)
    o = await e.submit(r, token, "BUY", price, size, "ENTRY")
    await e.accept_fills([asdict(Fill("fill-" + o["local_id"], o["local_id"], price, size, "0", "actual", e.api.now(), "MAKER"))])
    return o


async def test_dual_preorders_and_no_repeat(engine):
    await engine.manage()
    buys = list(engine.orders.values())
    assert len(buys) == 2
    assert {o["asset_id"] for o in buys} == {"up", "down"}
    assert all(o["price"] == "0.10" and o["size"] == "10" for o in buys)
    await engine.manage()
    assert len(engine.orders) == 2


async def test_deadline_cancels_both(engine):
    await engine.manage()
    engine.api.value = round_(engine)["deadline"]
    await engine.manage()
    assert all(o["status"] == "CANCELED" for o in engine.orders.values())


async def test_partial_fill_protects_and_cancels_other(engine):
    await engine.manage()
    o = next(o for o in engine.orders.values() if o["asset_id"] == "up")
    await engine.accept_fills([asdict(Fill("partial", o["local_id"], "0.10", "6", "0", "actual", engine.api.now(), "MAKER"))])
    await engine.manage()
    sellers = [o for o in engine.orders.values() if o["side"] == "SELL"]
    assert len(sellers) == 1
    assert sellers[0]["size"] == "6.00" and sellers[0]["price"] == "0.20"
    assert all(o["status"] == "CANCELED" for o in engine.orders.values() if o["side"] == "BUY")
    await engine.manage()
    assert len([o for o in engine.orders.values() if o["side"] == "SELL"]) == 1


async def test_fill_during_cancel_is_still_protected(engine):
    await engine.manage()
    target = next(iter(engine.orders.values()))
    original = engine.broker.cancel
    async def race(o):
        if o is target:
            await engine.accept_fills([asdict(Fill("late", o["local_id"], "0.10", "10", "0", "actual", engine.api.now(), "MAKER"))])
        return await original(o)
    engine.broker.cancel = race
    engine.api.value = round_(engine)["deadline"]
    await engine.manage()
    await engine.manage()
    assert any(o["side"] == "SELL" and D(o["size"]) == 10 for o in engine.orders.values())


async def test_both_sides_fill_protected_separately(engine):
    await buy(engine, "up")
    await buy(engine, "down")
    await engine.manage()
    assert {o["asset_id"] for o in engine.orders.values() if o["side"] == "SELL"} == {"up", "down"}


async def test_unknown_exit_not_resent(engine):
    await buy(engine)
    original = engine.broker.submit
    async def timeout(o, *args):
        if o["side"] == "SELL":
            raise TimeoutError()
        return await original(o, *args)
    engine.broker.submit = timeout
    await engine.manage()
    await engine.manage()
    exits = [o for o in engine.orders.values() if o["side"] == "SELL"]
    assert len(exits) == 1 and exits[0]["status"] == "UNKNOWN"


async def test_unknown_entry_stops_other_direction(engine):
    async def timeout(*args):
        raise TimeoutError()
    engine.broker.submit = timeout
    await engine.manage()
    await engine.manage()
    assert len(engine.orders) == 1
    assert next(iter(engine.orders.values()))["status"] in {"UNKNOWN", "CANCEL_PENDING"}


async def test_disabled_cancels_entry_but_keeps_exit(engine):
    await engine.manage()
    o = next(iter(engine.orders.values()))
    await engine.accept_fills([asdict(Fill("filled", o["local_id"], "0.10", "10", "0", "actual", engine.api.now(), "MAKER"))])
    engine.config.markets = [replace(engine.config.markets[0], enabled=False)]
    await engine.manage()
    assert any(o["side"] == "SELL" and o["status"] == "LIVE" for o in engine.orders.values())
    assert all(o["status"] in {"CANCELED", "FILLED"} for o in engine.orders.values() if o["side"] == "BUY")


async def test_start_after_open_does_not_enter(engine):
    engine.api.value = round_(engine)["market"]["start"] + 1
    await engine.manage()
    assert not engine.orders
    assert round_(engine)["skip_reason"] == "错过开盘前预挂窗口"


async def test_stale_book_blocks_entry(engine):
    for b in engine.feed.books.values():
        b.received_at = time.time() - 100
    await engine.manage()
    assert not engine.orders


async def test_clock_skew_blocks_entry(engine):
    engine.api.offset = 10
    await engine.manage()
    assert not engine.orders


async def test_risk_includes_both_directions(engine):
    engine.config.settings["max_open_exposure"] = "1.5"
    await engine.manage()
    assert not engine.orders


async def test_price_touch_is_not_simulated_fill(engine):
    engine.feed.books["up"].asks = {D("0.10"): D("2")}
    await engine.manage()
    await engine.reconcile()
    assert D(engine.position("test-round", "up")["shares"]) == 2
    # 盘口重复出现同样深度，不重复消费。
    await engine.reconcile()
    assert D(engine.position("test-round", "up")["shares"]) == 2


async def test_queue_requires_real_volume(engine):
    engine.feed.books["up"].bids[D("0.10")] = D(20)
    await engine.manage()
    await engine.reconcile()
    msg = {"payload": {"asset_id": "up", "price": "0.10", "size": "25", "side": "SELL"}, "timestamp": engine.api.now() + 1}
    engine.broker.on_trade(msg)
    await engine.reconcile()
    assert D(engine.position("test-round", "up")["shares"]) == 5


async def test_duplicate_fill_idempotent(engine):
    o = await buy(engine)
    fill = engine.fills[0]
    await engine.accept_fills([fill, fill])
    assert len(engine.fills) == 1
    assert D(engine.position("test-round", "up")["shares"]) == 10


async def test_restart_keeps_entry_and_exit(engine):
    await engine.manage()
    o = next(iter(engine.orders.values()))
    await engine.accept_fills([asdict(Fill("f", o["local_id"], "0.10", "10", "0", "actual", engine.api.now(), "MAKER"))])
    await engine.manage()
    engine.save()
    restarted = Engine(engine.db, engine.config, engine.api, engine.feed, engine.log)
    restarted.broker = PaperBroker(restarted)
    before = len(restarted.orders)
    await restarted.manage()
    assert len(restarted.orders) == before


async def test_partial_below_minimum_logged_not_falsely_protected(engine):
    await buy(engine, size="5")
    engine.feed.books["up"].min_size = "6"
    await engine.manage()
    assert not any(o["side"] == "SELL" for o in engine.orders.values())


async def test_old_and_next_round_coexist(engine):
    await buy(engine)
    old = engine.market("test-round")
    next_market = replace(old, slug="next-round", start=old.end, end=old.end + 900, tokens={"UP":"next-up", "DOWN":"next-down"})
    engine.add_round(next_market, engine.config.markets[0])
    assert len(engine.rounds) == 2
    assert D(engine.position("test-round", "up")["shares"]) == 10


async def test_no_early_settlement(engine):
    await buy(engine)
    await engine.settle()
    assert round_(engine)["settlement"] is None


async def test_final_resolution_payout(engine):
    await buy(engine)
    engine.api.value = round_(engine)["market"]["end"] + 1
    async def resolved(slug):
        return {"closed": True, "umaResolutionStatus": "resolved", "outcomes": '["Up","Down"]', "outcomePrices": '["1","0"]'}
    engine.api.market = resolved
    await engine.manage()
    await engine.settle()
    assert round_(engine)["status"] == "FINAL"
    assert D(engine.position("test-round", "up")["settlement_pnl"]) == 9


async def test_waiting_resolution_is_not_loss(engine):
    await buy(engine)
    engine.api.value = round_(engine)["market"]["end"] + 1
    async def unresolved(slug):
        return {"closed": True, "umaResolutionStatus": "proposed"}
    engine.api.market = unresolved
    await engine.manage()
    await engine.settle()
    assert round_(engine)["status"] == "WAIT_SETTLEMENT"
    assert round_(engine)["settlement"] is None
