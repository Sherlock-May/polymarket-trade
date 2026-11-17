from dataclasses import asdict

from bot.logger import simple_message, trade_message
from bot.models import Fill


async def test_order_and_cancel_confirmation_include_round_and_deadline(engine):
    r = engine.rounds['test-round']
    o = await engine.submit(r, 'up', 'BUY', '0.10', '10', 'ENTRY')
    message = simple_message(engine.db, '订单被接受', {'order': o['local_id']}, r['deadline'] - 1)
    assert '挂单成功' in message and 'BTC-4H' in message and 'UP' in message and '10 份' in message
    data = {'order': o['local_id'], 'matched': '2'}
    assert '按时' in simple_message(engine.db, '撤单已确认', data, r['deadline'] - 1)
    assert '超时' in simple_message(engine.db, '撤单已确认', data, r['deadline'] + 1)


async def test_final_loss_deducts_estimated_fee(engine):
    r = engine.rounds['test-round']
    o = await engine.submit(r, 'up', 'BUY', '0.10', '10', 'ENTRY')
    await engine.accept_fills([asdict(Fill('f', o['local_id'], '0.10', '10', '0.05', 'estimated', engine.api.now(), 'TAKER'))])
    r['settlement'] = {'payouts': {'up': '0', 'down': '1'}, 'confirmed_at': engine.api.now()}
    engine.save()
    text = simple_message(engine.db, '最终结算已确认', {'slug': 'test-round'}, engine.api.now())
    assert '归零' in text and '净收益 -1.05' in text and '手续费 0.05（含估算）' in text


async def test_sold_profit_reports_net_after_both_fees(engine):
    r = engine.rounds['test-round']
    buy = await engine.submit(r, 'up', 'BUY', '0.10', '10', 'ENTRY')
    sell = await engine.submit(r, 'up', 'SELL', '0.20', '10', 'TAKE_PROFIT')
    await engine.accept_fills([
        asdict(Fill('b', buy['local_id'], '0.10', '10', '0.05', 'actual', engine.api.now(), 'TAKER')),
        asdict(Fill('s', sell['local_id'], '0.20', '10', '0.10', 'estimated', engine.api.now() + 1, 'TAKER')),
    ])
    text = simple_message(engine.db, '本轮结束', {'slug': 'test-round'}, engine.api.now())
    assert '回收至少两倍本金' in text and '净收益 +0.85' in text and '手续费 0.15（含估算）' in text


async def test_trade_log_links_confirmed_buy_and_exit_order(engine):
    r = engine.rounds['test-round']
    buy = await engine.submit(r, 'up', 'BUY', '.10', '10', 'ENTRY')
    sell = await engine.submit(r, 'up', 'SELL', '.20', '10', 'TAKE_PROFIT')
    data = {'order': buy['local_id'], 'side':'BUY', 'matched_at':engine.api.now()}
    text = trade_message(engine.db, '真实成交', data, '买入成交')
    assert '实际撮合' in text and buy['local_id'] in text and '止盈结果待后续确认' in text
    text = trade_message(engine.db, '订单被接受', {'order':sell['local_id']}, '挂单成功')
    assert buy['local_id'] in text and sell['local_id'] in text
    assert trade_message(engine.db, '订单被接受', {'order':buy['local_id']}, '挂单成功') is None
