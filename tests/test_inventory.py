from dataclasses import asdict
from bot.config import D
from bot.models import Fill


async def test_matched_unconfirmed_sell_still_reserves_inventory(engine):
    r = engine.rounds["test-round"]
    buy = await engine.submit(r, "up", "BUY", "0.10", "10", "ENTRY")
    await engine.accept_fills([asdict(Fill("b", buy["local_id"], "0.10", "10", "0", "actual", engine.api.now(), "MAKER"))])
    await engine.manage()
    sell = next(o for o in engine.orders.values() if o["side"] == "SELL")
    sell["status"], sell["matched"] = "FILLED", "10"
    await engine.manage()
    assert len([o for o in engine.orders.values() if o["side"] == "SELL"]) == 1
    assert engine.reserved("SELL", "up") == 10
    await engine.accept_fills([asdict(Fill("s", sell["local_id"], "0.20", "10", "0", "actual", engine.api.now() + 1, "MAKER"))])
    assert engine.reserved("SELL", "up") == 0
    assert D(engine.position("test-round", "up")["shares"]) == 0


async def test_partial_sell_does_not_double_sell(engine):
    r = engine.rounds["test-round"]
    buy = await engine.submit(r, "up", "BUY", "0.10", "10", "ENTRY")
    await engine.accept_fills([asdict(Fill("b", buy["local_id"], "0.10", "10", "0", "actual", engine.api.now(), "MAKER"))])
    await engine.manage()
    sell = next(o for o in engine.orders.values() if o["side"] == "SELL")
    sell["matched"], sell["status"] = "6", "PARTIAL"
    await engine.manage()
    assert len([o for o in engine.orders.values() if o["side"] == "SELL"]) == 1


async def test_additional_buy_after_cancel_only_protects_new_quantity(engine):
    r = engine.rounds["test-round"]
    buy = await engine.submit(r, "up", "BUY", "0.10", "10", "ENTRY")
    await engine.accept_fills([asdict(Fill("b1", buy["local_id"], "0.10", "5", "0", "actual", engine.api.now(), "MAKER"))])
    await engine.manage()
    await engine.accept_fills([asdict(Fill("b2", buy["local_id"], "0.10", "5", "0", "actual", engine.api.now() + 1, "MAKER"))])
    await engine.manage()
    assert sum((D(o["size"]) for o in engine.orders.values() if o["side"] == "SELL"), D(0)) == 10
