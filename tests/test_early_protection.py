import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace as NS
import pytest

from bot.config import D
from bot.models import Fill
from bot.polymarket_client import LiveBroker
from test_live_adapter import Pages


async def setup_live(engine, balances):
    order = await engine.submit(engine.rounds['test-round'], 'up', 'BUY', '.10', '10', 'ENTRY')
    order['exchange_id'] = 'buy'
    posts = []
    class Client:
        async def get_balance_allowance(self, **kwargs):
            value = balances.pop(0) if len(balances) > 1 else balances[0]
            return NS(balance=value, allowances={'spender': 100000000})
        async def create_limit_order(self, **kwargs):
            return kwargs
        async def post_order(self, signed):
            posts.append(signed)
            return NS(ok=True, order_id='sell')
    engine.mode = 'live'
    engine.broker = LiveBroker(engine, Client())
    return order, posts


def event(engine, ident='trade', size='10', status='MATCHED'):
    return NS(type='trade', received_at=engine.api.now(), payload=NS(
        id=ident, taker_order_id='other', size=D(size), price=D('.10'),
        maker_orders=[NS(order_id='buy', price=D('.10'), matched_amount=D(size))],
        status=status, matched_at=datetime.fromtimestamp(engine.api.now(), timezone.utc), transaction_hash='tx'))


async def finish(engine):
    await asyncio.wait_for(asyncio.gather(*engine.fast_protect_tasks.values()), 2)


async def test_match_checks_balance_then_sells_before_confirmation(engine):
    order, posts = await setup_live(engine, [0, 10000000])
    del engine.rounds['test-round']['rule']['take_profit_enabled']  # 历史轮次默认继续止盈
    matched = event(engine)
    await engine.on_message('user_update', matched)
    await engine.on_message('user_update', matched)
    await finish(engine)
    assert len(posts) == 1 and posts[0]['size'] == D(10)
    assert engine.fills == []
    assert engine.unconfirmed_buy_shares('test-round', 'up') == 10
    await engine.on_message('user_update', event(engine, status='CONFIRMED'))
    await finish(engine)
    assert len(posts) == 1 and len(engine.fills) == 1
    assert engine.unconfirmed_buy_shares('test-round', 'up') == 0


async def test_live_take_profit_disabled_still_records_confirmed_buys(engine):
    order, posts = await setup_live(engine, [10000000])
    r = engine.rounds['test-round']
    r['rule']['take_profit_enabled'] = False
    async def unexpected_balance_check(*args):
        pytest.fail('disabled take profit should not check sellable balance')
    engine.broker.available = unexpected_balance_check

    await engine.on_message('user_update', event(engine))
    await engine.on_message('user_update', event(engine, status='CONFIRMED'))
    await engine.protect(r)
    assert engine.fast_protect_tasks == {}
    assert posts == [] and len(engine.fills) == 1
    assert D(engine.position('test-round', 'up')['shares']) == 10


async def test_failed_match_never_sells_and_ignores_stale_match(engine):
    order, posts = await setup_live(engine, [0])
    await engine.on_message('user_update', event(engine))
    await asyncio.sleep(0)
    await engine.on_message('user_update', event(engine, status='FAILED'))
    await engine.on_message('user_update', event(engine))
    await finish(engine)
    assert posts == [] and engine.fills == []
    assert engine.unconfirmed_buy_shares('test-round', 'up') == 0


async def test_partial_matches_combine_without_duplicate_inventory(engine):
    order, posts = await setup_live(engine, [5000000])
    await engine.on_message('user_update', event(engine, 'a', '2'))
    await finish(engine)
    assert posts == []
    await engine.on_message('user_update', event(engine, 'b', '3'))
    await engine.on_message('user_update', event(engine, 'a', '2'))
    await finish(engine)
    assert len(posts) == 1 and posts[0]['size'] == D(5)
    assert engine.fills == []


async def test_live_buy_remainder_is_kept_until_cumulative_matches_reach_market_minimum(engine):
    buy, posts = await setup_live(engine, [6000000])
    canceled = []
    async def cancel(order):
        canceled.append(order["local_id"])
        return True
    engine.broker.cancel = cancel
    engine.feed.books['up'].min_size = '6'

    await engine.on_message('user_update', event(engine, 'first', '5'))
    await engine.manage()
    assert canceled == [] and posts == []

    await engine.on_message('user_update', event(engine, 'second', '1'))
    await finish(engine)
    await engine.manage()
    assert canceled == [buy["local_id"]]
    assert len(posts) == 1 and posts[0]['size'] == D(6)
    assert engine.fills == []


@pytest.mark.parametrize('delay', [0, 1])
async def test_early_exit_confirmation_waits_for_buy_accounting(engine, delay):
    buy, posts = await setup_live(engine, [10000000])
    matched = event(engine)
    await engine.on_message('user_update', matched)
    await finish(engine)
    sell = next(o for o in engine.orders.values() if o['side'] == 'SELL')
    sold_at = matched.payload.matched_at.timestamp() + delay
    sold = asdict(Fill('aaa-sell-fill', sell['local_id'], '.20', '10', '0', 'actual', sold_at, 'MAKER'))
    await engine.accept_fills([sold])
    assert not engine.fills and 'aaa-sell-fill' in engine.deferred_sell_fills
    await engine.on_message('user_update', event(engine, status='CONFIRMED'))
    await finish(engine)
    assert len(engine.fills) == 2
    assert engine.position('test-round', 'up')['shares'] == '0'
    assert D(engine.position('test-round', 'up')['realized_pnl']) == 1


async def test_blocked_exit_network_does_not_hold_account_messages(engine):
    buy, posts = await setup_live(engine, [10000000])
    entered = asyncio.Event()
    async def slow_available(*args):
        entered.set()
        await asyncio.Event().wait()
    engine.broker.available = slow_available
    task = asyncio.create_task(engine.account_worker())
    try:
        engine.user_queue.put_nowait(('user_update', event(engine)))
        await asyncio.wait_for(entered.wait(), 1)
        engine.user_queue.put_nowait(('user_connected', {}))
        for _ in range(10):
            await asyncio.sleep(0)
            if engine.user_connected: break
        assert engine.user_connected
    finally:
        task.cancel()
        for worker in engine.fast_protect_tasks.values(): worker.cancel()
        await asyncio.gather(task, *engine.fast_protect_tasks.values(), return_exceptions=True)


async def test_rest_match_protects_while_order_details_are_blocked(engine):
    buy, posts = await setup_live(engine, [10000000])
    entered = asyncio.Event()
    client = engine.broker.client
    client.list_open_orders = lambda **kwargs: Pages([])
    client.list_account_trades = lambda **kwargs: Pages([event(engine).payload])
    async def get_order(**kwargs):
        entered.set()
        await asyncio.Event().wait()
    client.get_order = get_order
    query = asyncio.create_task(engine.broker.reconcile(archived=False))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await finish(engine)
        assert len(posts) == 1 and not query.done()
        assert engine.fills == []
    finally:
        query.cancel()
        await asyncio.gather(query, return_exceptions=True)


async def test_balance_and_allowance_remain_required_for_early_sell(engine):
    buy, posts = await setup_live(engine, [10000000])
    async def unavailable(*args): return D(10), False
    engine.broker.available = unavailable
    engine.observe_matched_trade(event(engine).payload, [buy])
    try:
        await engine.protect(engine.rounds['test-round'])
        assert posts == [] and engine.fills == []
    finally:
        for task in engine.fast_protect_tasks.values(): task.cancel()
        await asyncio.gather(*engine.fast_protect_tasks.values(), return_exceptions=True)
