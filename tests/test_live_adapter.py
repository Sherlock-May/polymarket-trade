from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace as NS
import pytest
from bot.config import D
from bot.models import Fill
from bot.polymarket_client import LiveBroker, OrderUnavailableError


class Pages:
    def __init__(self, items):
        self.items = items
    async def iter_items(self):
        for item in self.items:
            yield item


async def test_live_sign_and_post_separate_no_auto_approval(engine):
    calls = []
    class Client:
        async def create_limit_order(self, **kwargs):
            calls.append(("sign", kwargs))
            return "signed"
        async def post_order(self, signed):
            calls.append(("post", signed))
            return NS(ok=True, order_id="server-order")
    broker = LiveBroker(engine, Client())
    r = engine.rounds["test-round"]
    order = await engine.submit(r, "up", "BUY", "0.10", "10", "ENTRY")
    response = await broker.submit(order, engine.market("test-round"), r["deadline"])
    assert response["exchange_id"] == "server-order"
    assert calls[0][1]["price"] == D("0.10")
    assert calls[0][1]["expiration"] == int(r["deadline"] + 60)
    assert calls[1] == ("post", "signed")


async def test_live_does_not_post_if_signing_crosses_open(engine):
    class Client:
        async def create_limit_order(self, **kwargs):
            engine.api.value = engine.market("test-round").start
            return "signed"
        async def post_order(self, signed):
            pytest.fail("开盘后不得发起预挂")
    broker = LiveBroker(engine, Client())
    order = await engine.submit(engine.rounds["test-round"], "up", "BUY", "0.10", "10", "ENTRY")
    result = await broker.submit(order, engine.market("test-round"))
    assert result["status"] == "REJECTED"


async def test_balance_base_units(engine):
    class Client:
        async def get_balance_allowance(self, **kwargs):
            return NS(balance=12345678, allowances={"spender": 999999999})
    amount, allowed = await LiveBroker(engine, Client()).available()
    assert amount == D("12.345678") and allowed


async def test_cancel_requires_final_confirmation(engine):
    class Client:
        async def cancel_order(self, **kwargs):
            return NS(canceled=["server"])
        async def get_order(self, **kwargs):
            return NS(status="MATCHED", size_matched=D(10), original_size=D(10))
    o = {"exchange_id":"server", "status":"CANCEL_PENDING"}
    assert await LiveBroker(engine, Client()).cancel(o) is False
    assert o["status"] == "FILLED" and o["matched"] == "10"


async def test_pending_trade_closes_entry_without_faking_fill(engine):
    order = await engine.submit(engine.rounds["test-round"], "up", "BUY", "0.10", "10", "ENTRY")
    order["exchange_id"] = "server"
    class Client:
        def list_open_orders(self, **kwargs):
            return Pages([])
        async def get_order(self, **kwargs):
            return NS(status="MATCHED", size_matched=D(10), original_size=D(10))
        def list_account_trades(self, **kwargs):
            return Pages([NS(id="trade", taker_order_id="server", maker_orders=[], price=D("0.10"), size=D(10), status="MATCHED")])
    fills = await LiveBroker(engine, Client()).reconcile()
    assert fills == []
    assert engine.rounds["test-round"]["entry_closed"]
    assert engine.pending_inventory() == 1


async def test_confirmed_trade_fee_marked_estimate(engine):
    order = await engine.submit(engine.rounds["test-round"], "up", "BUY", "0.10", "10", "ENTRY")
    order["exchange_id"] = "server"
    class Client:
        def list_open_orders(self, **kwargs):
            return Pages([])
        async def get_order(self, **kwargs):
            return NS(status="MATCHED", size_matched=D(10), original_size=D(10))
        def list_account_trades(self, **kwargs):
            return Pages([NS(id="trade", taker_order_id="server", maker_orders=[], price=D("0.10"), size=D(10), status="CONFIRMED", matched_at=datetime.now(timezone.utc), transaction_hash="tx")])
    fills = await LiveBroker(engine, Client()).reconcile()
    assert fills[0]["fee_kind"] == "estimated"
    assert D(fills[0]["fee"]) == D("0.063")


async def test_no_market_orders_ever(engine):
    await engine.manage()
    assert all(o["price"] == "0.10" for o in engine.orders.values())


def null_order_response():
    from polymarket.models.clob.account import OpenOrder
    return OpenOrder.parse_response(None)


async def test_null_order_uses_exact_id_fallback(engine):
    remote = NS(id="server", status="CANCELED")
    class Client:
        async def get_order(self, **kwargs):
            return null_order_response()
        def list_open_orders(self, **kwargs):
            assert kwargs == {"id": "server"}
            return Pages([remote])
    assert await LiveBroker(engine, Client())._get_order("server") is remote


@pytest.mark.parametrize("items", [[], [NS(id="other")], [NS(id="server"), NS(id="server")]])
async def test_null_order_never_claims_missing_or_ambiguous_order(engine, items):
    class Client:
        async def get_order(self, **kwargs):
            return null_order_response()
        def list_open_orders(self, **kwargs):
            return Pages(items)
    with pytest.raises(OrderUnavailableError):
        await LiveBroker(engine, Client())._get_order("server")


@pytest.mark.parametrize("block", [None, "not_expired", "inventory", "trade", "fill", "matched", "unknown", "sell", "open"])
async def test_missing_gtd_requires_all_expiry_evidence(engine, block):
    r = engine.rounds["test-round"]
    order = await engine.submit(r, "up", "BUY", "0.10", "10", "ENTRY")
    order.update(exchange_id="server", status="CANCEL_PENDING", expires_at=r["deadline"] + 60)
    engine.api.value = r["market"]["end"] + 120
    opened, trades = [], []
    class Client:
        async def get_balance_allowance(self, **kwargs):
            return NS(balance=1 if block == "inventory" else 0, allowances={})
    if block == "not_expired": engine.api.value = r["deadline"]
    if block == "trade": trades = [NS(taker_order_id="server", maker_orders=[])]
    if block == "fill": engine.fills.append({"order_id": order["local_id"]})
    if block == "matched": order["matched"] = "1"
    if block == "unknown": order["status"] = "UNKNOWN"
    if block == "sell": order["side"] = "SELL"
    if block == "open": opened = [NS(id="server")]
    result = await LiveBroker(engine, Client())._expired_without_inventory(order, r, opened, trades)
    assert result == (block is None)
    assert (order["status"] == "EXPIRED") == (block is None)


async def test_reconcile_recovers_null_expired_gtd_without_writing_exchange(engine):
    r = engine.rounds["test-round"]
    order = await engine.submit(r, "up", "BUY", "0.10", "10", "ENTRY")
    order.update(exchange_id="server", status="CANCEL_PENDING")
    engine.api.value = r["market"]["end"] + 120
    class Client:
        async def get_order(self, **kwargs):
            return null_order_response()
        def list_open_orders(self, **kwargs): return Pages([])
        def list_account_trades(self, **kwargs): return Pages([])
        async def get_balance_allowance(self, **kwargs):
            return NS(balance=0, allowances={})
    engine.broker = LiveBroker(engine, Client())
    await engine.broker.reconcile(archived=True)
    await engine.reconcile()
    assert engine.connection_reconciled
    assert order["status"] == "EXPIRED" and engine.exposure() == 0
    assert engine.fills == []


@pytest.mark.parametrize("found", [True, False])
async def test_legacy_pending_without_exchange_id_is_reconciled_or_blocks(engine, found):
    order = await engine.submit(engine.rounds["test-round"], "up", "BUY", "0.10", "10", "ENTRY")
    order.update(exchange_id=None, status="CANCEL_PENDING")
    remote = NS(id="server", asset_id="up", side="BUY", price=D("0.10"), original_size=D("10"),
                size_matched=D("0"), status="LIVE", created_at=datetime.fromtimestamp(order["created_at"], timezone.utc))
    class Client:
        def list_open_orders(self, **kwargs): return Pages([remote] if found else [])
        def list_account_trades(self, **kwargs): return Pages([])
        async def get_order(self, **kwargs): return remote
    engine.mode = "live"
    engine.broker = LiveBroker(engine, Client())
    await engine.reconcile()
    assert engine.connection_reconciled
    assert order.get("exchange_id") == ("server" if found else None)
    if not found:
        await engine.cancel(order, "test")
        assert order["status"] == "UNKNOWN"


@pytest.mark.parametrize("after_send", [False, True])
async def test_live_failure_distinguishes_before_and_after_submission(engine, after_send):
    engine.mode = "live"
    class Broker:
        async def submit(self, order, market, deadline):
            if after_send:
                order["status"] = "UNKNOWN"
            raise TimeoutError()
    engine.broker = Broker()
    order = await engine.submit(engine.rounds["test-round"], "up", "BUY", "0.10", "10", "ENTRY")
    assert order["status"] == ("UNKNOWN" if after_send else "REJECTED")


async def test_live_confirmed_fill_places_exit_before_manage_and_does_not_duplicate(engine):
    order = await engine.submit(engine.rounds['test-round'], 'up', 'BUY', '0.10', '10', 'ENTRY')
    engine.mode = 'live'
    fill = asdict(Fill('priority', order['local_id'], '0.10', '10', '0', 'actual', engine.api.now(), 'MAKER'))
    await engine.accept_fills([fill])
    exits = [o for o in engine.orders.values() if o['side'] == 'SELL']
    assert len(exits) == 1 and exits[0]['price'] == '0.20'
    assert exits[0]['size'] == '10.00'
    await engine.accept_fills([fill])
    await engine.protect(engine.rounds['test-round'])
    assert len([o for o in engine.orders.values() if o['side'] == 'SELL']) == 1


@pytest.mark.parametrize('status', ['CONFIRMED', 'MATCHED'])
async def test_authenticated_push_can_protect_without_full_rest_reconcile(engine, status):
    order = await engine.submit(engine.rounds['test-round'], 'up', 'BUY', '0.10', '10', 'ENTRY')
    order['exchange_id'] = 'server-buy'
    posts = []
    class Client:
        async def get_balance_allowance(self, **kwargs):
            return NS(balance=10000000, allowances={'spender':10000000})
        async def create_limit_order(self, **kwargs):
            assert kwargs['side'] == 'SELL' and kwargs['size'] == D('10')
            return 'signed-exit'
        async def post_order(self, signed):
            posts.append(signed)
            return NS(ok=True, order_id='server-exit')
    engine.mode = 'live'
    engine.broker = LiveBroker(engine, Client())
    trade = NS(id='push-trade', taker_order_id='other', maker_orders=[NS(order_id='server-buy', price=D('.10'), matched_amount=D('10'))],
               status=status, matched_at=datetime.fromtimestamp(engine.api.now(), timezone.utc), transaction_hash='tx')
    event = NS(type='trade', payload=trade)
    await engine.on_message('user_update', event)
    await engine.on_message('user_update', event)
    import asyncio
    await asyncio.gather(*engine.fast_protect_tasks.values())
    if status == 'CONFIRMED':
        assert posts == ['signed-exit']
        await engine.accept_fills(engine.broker.trade_fills(trade, list(engine.orders.values())))
        assert posts == ['signed-exit'] and len(engine.fills) == 1
    else:
        assert posts == ['signed-exit'] and engine.fills == []


async def test_concurrent_exit_checks_cannot_reserve_same_inventory(engine):
    import asyncio
    order = await engine.submit(engine.rounds['test-round'], 'up', 'BUY', '0.10', '10', 'ENTRY')
    await engine.accept_fills([asdict(Fill('inventory', order['local_id'], '.10', '10', '0', 'actual', engine.api.now(), 'MAKER'))])
    original = engine.broker.available
    async def slow_available(*args):
        await asyncio.sleep(.01)
        return await original(*args)
    engine.broker.available = slow_available
    await asyncio.gather(engine.protect(engine.rounds['test-round']), engine.protect(engine.rounds['test-round']))
    assert len([o for o in engine.orders.values() if o['side'] == 'SELL']) == 1


@pytest.mark.parametrize('sent', [False, True])
async def test_shutdown_during_exit_submission_preserves_uncertainty(engine, sent):
    import asyncio
    engine.mode = 'live'
    class Broker:
        async def submit(self, order, market, deadline):
            if sent:
                order['status'] = 'UNKNOWN'
            raise asyncio.CancelledError()
    engine.broker = Broker()
    with pytest.raises(asyncio.CancelledError):
        await engine.submit(engine.rounds['test-round'], 'up', 'SELL', '.20', '10', 'TAKE_PROFIT')
    order = next(iter(engine.orders.values()))
    assert order['status'] == ('UNKNOWN' if sent else 'REJECTED')
    assert engine.db.orders()[0]['status'] == order['status']
