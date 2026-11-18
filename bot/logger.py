import logging
import os
import time
import traceback
import json
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo
from .database import dumps


def error_fields(exc):
    """保留定位信息，不记录异常原文、请求或响应值。"""
    fields = {"error_type": type(exc).__name__}
    frames = traceback.extract_tb(exc.__traceback__)
    if frames:
        fields["error_location"] = f"{Path(frames[-1].filename).name}:{frames[-1].name}:{frames[-1].lineno}"
    from pydantic import ValidationError
    cause = exc.__cause__
    if isinstance(cause, ValidationError):
        fields["validation_errors"] = [
            {"field": ".".join(str(p) for p in e["loc"] if isinstance(p, int) or str(p).isidentifier()),
             "type": e["type"]}
            for e in cause.errors(include_input=False, include_context=False, include_url=False)
        ][:10]
    return fields


class ZonedFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, ZoneInfo(os.getenv("LOG_TIMEZONE", "Asia/Shanghai"))).isoformat(timespec="seconds")


def setup(mode):
    Path("logs").mkdir(exist_ok=True)
    log = logging.getLogger("bot")
    log.setLevel(os.getenv("LOG_LEVEL", "INFO"))
    formatter = ZonedFormatter("[%(asctime)s] %(levelname)s %(message)s")
    if not log.handlers:
        handler = RotatingFileHandler(f"logs/{mode}.log", maxBytes=10_000_000, backupCount=10, encoding="utf-8")
        handler.setFormatter(formatter)
        log.addHandler(handler)
    simple = logging.getLogger("bot.simple")
    simple.propagate = False
    simple.setLevel(logging.INFO)
    if not simple.handlers:
        for handler in (logging.StreamHandler(), RotatingFileHandler(f"logs/{mode}-simple.log", maxBytes=10_000_000, backupCount=10, encoding="utf-8")):
            handler.setFormatter(ZonedFormatter("[%(asctime)s] %(message)s"))
            simple.addHandler(handler)
    trades = logging.getLogger('bot.trades')
    trades.propagate = False
    trades.setLevel(logging.INFO)
    if not trades.handlers:
        handler = RotatingFileHandler(f'logs/{mode}-trades.log', maxBytes=10_000_000, backupCount=10, encoding='utf-8')
        handler.setFormatter(ZonedFormatter('[%(asctime)s] %(message)s'))
        trades.addHandler(handler)
    # SDK/httpx 请求日志可能含签名查询参数；禁止开启底层调试日志。
    for name in ("httpx", "httpcore", "websockets", "polymarket"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return log


def _local_time(timestamp):
    return datetime.fromtimestamp(timestamp, ZoneInfo(os.getenv("LOG_TIMEZONE", "Asia/Shanghai"))).strftime("%m-%d %H:%M:%S")


def simple_message(db, name, data, now):
    from .config import D
    from .pnl_calculator import position
    order_id = data.get("order")
    row = db.conn.execute("SELECT data FROM orders WHERE local_id=?", (order_id,)).fetchone() if order_id else None
    order = json.loads(row[0]) if row else None
    slug = data.get("slug") or (order and order["slug"]) or data.get("key")
    row = db.conn.execute("SELECT data FROM market_rounds WHERE slug=?", (slug,)).fetchone() if slug else None
    r = json.loads(row[0]) if row else None
    label = f"{r['rule']['id'].upper()} {_local_time(r['market']['start'])} 开盘" if r else str(slug or "")
    outcome = next((k for k, v in r["market"]["tokens"].items() if order and v == order["asset_id"]), "") if r else ""
    if name in {"程序启动", "配置已热更新"}:
        message = "启用市场：" + (", ".join(data.get("enabled", [])) or "无") + f" | 模式：{data.get('mode')}"
        if name == "程序启动":
            message += f" | PID {data.get('pid', '?')} | 停机诊断 v{data.get('stop_debug_version', 1)} | 限时 {data.get('run_seconds') or '无'}"
        return message
    if name == "发现下一轮市场" and r:
        return f"发现下一轮 | {label}，{_local_time(r['market']['end'])} 结束 | 买单截止 {_local_time(r['deadline'])}"
    if name in {"订单被接受", "订单被拒绝", "订单未发送", "提交结果未知禁止重复下单"} and order:
        result = {"订单被接受": "挂单成功", "订单被拒绝": "挂单被拒绝", "订单未发送": "挂单未发送", "提交结果未知禁止重复下单": "挂单结果未知，等待核实"}[name]
        purpose = "买入" if order["side"] == "BUY" else "止盈卖出"
        return f"{result} | {label} | {purpose} {outcome} | 价格 {order['price']} × {order['size']} 份" + (f" | 原因 {data.get('reason') or data.get('error_type')}" if data.get('reason') or data.get('error_type') else "")
    if name in {"真实成交", "模拟成交"} and r:
        direction = next((k for k, v in r['market']['tokens'].items() if v == data.get('asset')), '?')
        fee_kind = "实际" if data.get('fee_kind') == 'actual' else "估算"
        return f"{'买入' if data['side'] == 'BUY' else '卖出'}成交 | {label} | {direction} | 价格 {data['price']} × {data['shares']} 份 | 手续费 {data['fee']}（{fee_kind}）"
    if name == '止盈处理中':
        return f"止盈处理中 | {label} | 待保护 {data.get('shares')} 份，正在核实可卖余额"
    if name == '旧止盈单已结束':
        return f"旧止盈单已结束 | {label} | 市场已结算且无活动挂单，收益以成交及结算记录为准"
    if name == '恢复待预挂轮次':
        return f"恢复待预挂 | {label} | 上次退出前未下过单，重新等待预挂检查"
    if name == '收到撮合通知立即检查止盈':
        matched = _local_time(data['matched_at']) if data.get('matched_at') else '未提供'
        return f"收到撮合通知 | {label} | 订单 {order_id} | 状态 {data.get('status')} | 实际撮合 {matched} | 已触发可卖余额检查，尚不代表止盈成功"
    if name == '撮合失败需核实持仓':
        return f"撮合失败需核实持仓 | {label} | 订单 {order_id}"
    if name == '撮合到止盈提交结果':
        return f"撮合到止盈提交结果 | {label} | 止盈订单 {order_id} | 状态 {data.get('status')} | 自撮合约 {data.get('elapsed_ms')} 毫秒（含平台通知和余额等待）"
    if name == '持仓暂不可挂止盈':
        return f"止盈暂未挂出 | {label} | 可卖 {data.get('shares')} 份，账户余额 {data.get('balance', '?')} 份，最少 {data.get('min_size')} 份，授权 {'足够' if data.get('allowance_ok') else '不足'}"
    if name == '止盈处理耗时':
        return f"止盈处理返回 | {label} | 耗时 {data.get('milliseconds')} 毫秒（是否成功以挂单结果为准）"
    if name == '止盈重试暂停需检查':
        return f"止盈重试暂停 | {label} | 已有 {data.get('rejected')} 笔被拒，需检查拒单原因"
    if name == "撤单已确认" and order and r:
        if order['side'] != 'BUY':
            return f"卖单撤销已确认 | {label} | {outcome}"
        timing = '按时' if now <= r['deadline'] else '超时'
        return f"买单撤销已确认（{timing}） | {label} | {outcome} | 截止 {_local_time(r['deadline'])} | 已成交 {data.get('matched', '0')} 份"
    if name in {"最终结算已确认", "本轮结束"} and r:
        orders = {o['local_id']: o for o in db.orders() if o['slug'] == slug}
        fills = [f for f in db.fills() if f['order_id'] in orders]
        if not fills:
            return f"本轮结束 | {label} | 无成交" if name == '本轮结束' else None
        positions = [position(fills, orders, slug, token) for token in r['market']['tokens'].values()]
        if name == '本轮结束' and any(D(p['shares']) > 0 for p in positions):
            return None
        pnl = sum((D(p['realized_pnl']) for p in positions), D(0))
        if r.get('settlement'):
            pnl += sum((D(p['shares']) * D(r['settlement']['payouts'].get(token, 0)) - D(p['cost']) for token, p in zip(r['market']['tokens'].values(), positions)), D(0))
        fee = sum((D(f['fee']) for f in fills), D(0))
        estimate = any(f['fee_kind'] != 'actual' for f in fills)
        buy_value = sum((D(f['price']) * D(f['shares']) for f in fills if orders[f['order_id']]['side'] == 'BUY'), D(0))
        sell_value = sum((D(f['price']) * D(f['shares']) for f in fills if orders[f['order_id']]['side'] == 'SELL'), D(0))
        payout = sum((D(p['shares']) * D(r['settlement']['payouts'].get(token, 0)) for token, p in zip(r['market']['tokens'].values(), positions)), D(0)) if r.get('settlement') else D(0)
        outcome_text = '归零' if buy_value and sell_value + payout == 0 else ('回收至少两倍本金' if buy_value and sell_value + payout >= 2 * buy_value else ('结算' if name == '最终结算已确认' else '已卖出'))
        receivable = ' | 获胜金额为结算应收，到账以赎回为准' if r.get('settlement') and any(D(p['shares']) * D(r['settlement']['payouts'].get(token, 0)) > 0 for token, p in zip(r['market']['tokens'].values(), positions)) else ''
        return f"{outcome_text} | {label} | 净收益 {pnl:+} | 手续费 {fee}（{'含估算' if estimate else '实际'}）{receivable}"
    if name in {"暂缓预挂", "对账失败暂停新开仓", "时钟同步失败", "未知订单等待核实", "撤单尚未确认", "撤单异常需继续对账", "持仓暂不可挂止盈", "止盈保护异常", "运行异常暂停新增风险", "行情连接中断", "安全停机开始", "安全停机完成"}:
        return f"{name} | {label}" + (f" | {data.get('reason') or data.get('error_type')}" if data.get('reason') or data.get('error_type') else '')
    return None


def event(db, log, name, level="INFO", **data):
    # 调用处只传业务字段；不写原始认证事件、密钥、HTTP 请求或异常内容。
    log.log(getattr(logging, level), "%s %s", name, dumps(data))
    with db.transaction():
        db.conn.execute("INSERT INTO bot_events(timestamp,level,event,data) VALUES(?,?,?,?)", (time.time(), level, name, dumps(data)))
    try:
        message = simple_message(db, name, data, time.time())
        if message:
            logging.getLogger("bot.simple").log(getattr(logging, level), "%s", message)
        trade_text = trade_message(db, name, data, message)
        if trade_text:
            logging.getLogger('bot.trades').log(getattr(logging, level), '%s', trade_text)
    except Exception as exc:
        log.error("简洁日志生成失败 %s", dumps(error_fields(exc)))


def trade_message(db, name, data, message):
    if not message:
        return None
    if name in {'真实成交', '模拟成交'}:
        timestamp = data.get('matched_at')
        timing = _local_time(timestamp) if timestamp is not None else '未提供'
        result = '买入已成交，止盈结果待后续确认' if data['side'] == 'BUY' else '止盈卖出已成交（可能为部分成交）'
        return f"{result} | {message} | 订单 {data.get('order', '?')} | 实际撮合 {timing}"
    order_id = data.get('order')
    row = db.conn.execute('SELECT data FROM orders WHERE local_id=?', (order_id,)).fetchone() if order_id else None
    order = json.loads(row[0]) if row else None
    if order and order['purpose'] == 'TAKE_PROFIT' and name in {'订单被接受', '订单被拒绝', '订单未发送', '提交结果未知禁止重复下单', '撤单已确认'}:
        buys = [o['local_id'] for o in db.orders() if o['slug'] == order['slug'] and o['asset_id'] == order['asset_id'] and o['side'] == 'BUY']
        return f"{message} | 止盈订单 {order_id} | 对应买单 {','.join(buys)}"
    if name in {'撮合到止盈提交结果', '收到撮合通知立即检查止盈', '撮合失败需核实持仓', '止盈处理中', '持仓暂不可挂止盈', '止盈保护异常', '止盈处理耗时', '止盈重试暂停需检查', '最终结算已确认'}:
        return message
    return None
