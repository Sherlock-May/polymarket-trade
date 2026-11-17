from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sqlite3
import sys
import time
from dataclasses import asdict
from pathlib import Path
from dotenv import load_dotenv
from bot.config import Config, live_enabled
from bot.database import Database
from bot.engine import Engine
from bot.lock import ProcessLock
from bot.logger import setup
from bot.market_discovery import PublicAPI
from bot.models import Book, utc_iso
from bot.polymarket_client import LiveBroker, PaperBroker
from bot.websocket_client import MarketFeed, user_feed


def parser():
    p = argparse.ArgumentParser(description="Polymarket 多市场下一轮双向预挂机器人")
    p.add_argument("--config", default="markets.json", help="市场配置 JSON 路径")
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="校验配置，不访问账户")
    c = commands.add_parser("check", help="只读验证生产接口和下一轮市场")
    c.add_argument("--market", default=None)
    c.add_argument("--ws-seconds", type=int, default=10)
    r = commands.add_parser("run", help="运行；默认生产行情模拟")
    r.add_argument("--seconds", type=int, default=None, help="限定运行秒数后安全停止")
    r.add_argument("--record", action="store_true", help="保存公开市场消息，供回放")
    commands.add_parser("status", help="显示最近一次统计报告")
    commands.add_parser("report", help="从持久化数据重新导出报告")
    commands.add_parser("auth-check", help="显式读取账户认证/余额/已有订单，不提交交易")
    commands.add_parser("reconcile", help="备份数据库并核对实盘旧单，仅查询账户，不下单或撤单")
    m = commands.add_parser("market", help="显示或修改市场配置，运行中自动生效")
    m.add_argument("action", choices=["list", "enable", "disable", "set"])
    m.add_argument("id", nargs="?")
    m.add_argument("field", nargs="?", choices=["entry_price", "shares", "take_profit_price", "entry_window_seconds", "preopen_seconds"])
    m.add_argument("value", nargs="?")
    replay = commands.add_parser("replay", help="回放已保存的模拟行情和轮次，不访问网络")
    replay.add_argument("recording")
    replay.add_argument("--source-db", default="data/paper.db")
    replay.add_argument("--output-db", default="data/replay.db")
    return p


def market_command(args):
    config = Config.load(args.config)
    if args.action == "list":
        for rule in config.markets:
            print(f"{rule.id:10} {'开启' if rule.enabled else '关闭'}  买价={rule.entry_price} 每边份额={rule.shares} 止盈={rule.take_profit_price} 挂单时长={rule.entry_window_seconds}秒")
        return
    if not args.id:
        raise ValueError("请指定市场 id，例如 btc-4h")
    raw = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    match = [r for r in raw["markets"] if r["id"] == args.id]
    if len(match) != 1:
        raise ValueError("市场 id 不存在；先运行 market list")
    r = match[0]
    if args.action == "set":
        if args.field is None or args.value is None:
            raise ValueError("用法：market set btc-4h take_profit_price 0.20")
        r[args.field] = int(args.value) if args.field.endswith("seconds") else args.value
    else:
        r["enabled"] = args.action == "enable"
    path = Path(args.config)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    Config.load(tmp)
    tmp.replace(path)
    print(f"已保存 {args.id}；开关运行中自动生效，价格/份额/时间参数用于新发现的轮次。")


async def check(args, config):
    from polymarket import AsyncPublicClient
    api = PublicAPI()
    result = {"checked_at": utc_iso(time.time()), "kind": "production_read_only", "markets": []}
    try:
        result["clock_offset_seconds"] = await api.sync_clock()
        rules = [r for r in config.markets if r.id == args.market] if args.market else [r for r in config.markets if r.enabled]
        if not rules:
            raise ValueError("没有匹配的市场配置")
        async with AsyncPublicClient() as client:
            for rule in rules:
                try:
                    m = await api.discover(rule)
                    row = {"config": rule.id, "market": asdict(m) if m else None, "books": []}
                    if m:
                        for outcome, token in m.tokens.items():
                            raw = await api.book(token)
                            b = Book.parse(raw, time.time())
                            row["books"].append({"outcome": outcome, "asset_id": token, "best_bid": str(max(b.bids)) if b.bids else None,
                                                 "best_ask": str(min(b.asks)) if b.asks else None,
                                                 "min_size": b.min_size, "tick": b.tick, "book_timestamp": b.timestamp})
                        messages = []
                        async def emit(kind, data):
                            if kind == "market_message":
                                messages.append({"type": data["type"], "received_at": data["received_at"], "asset_id": data["payload"].get("asset_id")})
                        feed = MarketFeed(client, emit)
                        await feed.set_tokens(m.tokens.values())
                        await asyncio.sleep(max(0, args.ws_seconds))
                        await feed.close()
                        row["websocket"] = {"message_count": len(messages), "message_types": sorted({x["type"] for x in messages}),
                                            "received_assets": sorted({x["asset_id"] for x in messages if x["asset_id"]})}
                    result["markets"].append(row)
                except Exception as exc:
                    result["markets"].append({"config": rule.id, "error_type": type(exc).__name__, "verified": False})
        Path("reports").mkdir(exist_ok=True)
        Path("reports/api-check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if all(r.get("market") and r.get("websocket", {}).get("message_count", 0) > 0 for r in result["markets"]) else 2
    finally:
        await api.close()


async def run(args, config):
    from polymarket import AsyncPublicClient
    mode = "live" if live_enabled() else "paper"
    db_path = os.getenv("DATABASE_PATH") or f"data/{mode}.db"
    with ProcessLock(db_path):
        db = Database(db_path)
        log = setup(mode)
        api = PublicAPI()
        user_task = None
        async with AsyncPublicClient() as public:
            engine = None
            async def emit(kind, data):
                await engine.queue.put((kind, data))
            feed = MarketFeed(public, emit)
            try:
                engine = Engine(db, config, api, feed, log, mode)
                await api.sync_clock()
                if mode == "live":
                    geo = await api.geoblock()
                    if geo.get("blocked") is not False:
                        raise ValueError("平台地区检查未通过，不启动实盘")
                    engine.geo_allowed, engine.last_geo = True, api.now()
                    engine.broker = await LiveBroker.create(engine)
                    user_task = asyncio.create_task(user_feed(engine.broker.client, engine.user_queue))
                else:
                    engine.broker = PaperBroker(engine)
                if args.record:
                    Path("data/recordings").mkdir(parents=True, exist_ok=True)
                    engine.recording = Path(f"data/recordings/{int(time.time())}.jsonl").open("a", encoding="utf-8")
                def request_stop(signum, _frame):
                    engine.stop_reason = f"收到 {signal.Signals(signum).name} 信号"
                    engine.stopping = True
                for sig in (signal.SIGINT, signal.SIGTERM):
                    try:
                        signal.signal(sig, request_stop)
                    except ValueError:
                        pass
                engine.emit("程序启动", enabled=[r.id for r in config.markets if r.enabled], database=db_path,
                            pid=os.getpid(), parent_pid=os.getppid(), run_seconds=args.seconds,
                            stop_debug_version=2)
                await engine.run(args.config, args.seconds)
            finally:
                if user_task:
                    user_task.cancel()
                    await asyncio.gather(user_task, return_exceptions=True)
                await feed.close()
                if engine and engine.broker:
                    await engine.broker.close()
                if engine and engine.recording:
                    engine.recording.close()
                await api.close()
                db.close()


async def auth_check(config):
    if not os.getenv("POLYMARKET_PRIVATE_KEY") or not os.getenv("POLYMARKET_WALLET_ADDRESS"):
        raise ValueError("先在本地 .env 配置私钥和钱包地址，不要发送到聊天")
    # 不开启交易循环；SDK 可能创建/派生 API 凭据，但不下订单、不授权资产。
    stub = type("Stub", (), {})()
    broker = await LiveBroker.create(stub)
    try:
        cash, allowed = await broker.available()
        orders = [o async for o in broker.client.list_open_orders().iter_items()]
        print(json.dumps({"authenticated": True, "collateral_balance": str(cash), "allowance_sufficient": allowed,
                          "open_order_count": len(orders), "wallet_type": broker.client.wallet_type, "orders_submitted": 0}, ensure_ascii=False, indent=2))
    finally:
        await broker.close()


async def reconcile_account(config):
    if not live_enabled():
        raise ValueError("此命令用于实盘数据库；模拟盘使用正常运行对账")
    path = Path(os.getenv("DATABASE_PATH") or "data/live.db")
    if not path.is_file():
        raise ValueError("实盘数据库不存在，无旧单需要恢复")
    with ProcessLock(path):
        backup = path.with_name(f"{path.stem}-before-reconcile-{time.time_ns()}.db")
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(backup) as target:
                source.backup(target)
        db, api, broker = Database(path), PublicAPI(), None
        try:
            engine = Engine(db, config, api, type("Feed", (), {"books": {}})(), setup("live"), "live")
            await api.sync_clock()
            broker = engine.broker = await LiveBroker.create(engine)
            await engine.reconcile()
            from bot.reporting import report
            engine.stopping = True
            report(engine)
            print(json.dumps({"backup": str(backup), "reconciled": engine.connection_reconciled,
                              "cancel_pending": sum(o["status"] == "CANCEL_PENDING" for o in engine.orders.values()),
                              "expired": sum(o["status"] == "EXPIRED" for o in engine.orders.values()),
                              "orders_submitted": 0, "cancellations_submitted": 0}, ensure_ascii=False))
            return 0 if engine.connection_reconciled else 2
        finally:
            if broker:
                await broker.close()
            await api.close()
            db.close()


def main():
    load_dotenv(override=False)
    args = parser().parse_args()
    try:
        if args.command == "market":
            return market_command(args)
        config = Config.load(args.config)
        if args.command == "validate":
            print("配置有效；开启市场：" + ", ".join(r.id for r in config.markets if r.enabled))
        elif args.command == "check":
            return asyncio.run(check(args, config))
        elif args.command == "run":
            asyncio.run(run(args, config))
        elif args.command == "auth-check":
            asyncio.run(auth_check(config))
        elif args.command == "reconcile":
            return asyncio.run(reconcile_account(config))
        elif args.command == "status":
            mode = "live" if live_enabled() else "paper"
            print(Path(f"reports/{mode}/summary.json").read_text(encoding="utf-8"))
        elif args.command == "report":
            from bot.reporting import report
            import logging
            mode = "live" if live_enabled() else "paper"
            api = PublicAPI()
            feed = type("Feed", (), {"books": {}})()
            with ProcessLock(os.getenv("DATABASE_PATH") or f"data/{mode}.db"):
                db = Database(os.getenv("DATABASE_PATH") or f"data/{mode}.db")
                try:
                    e = Engine(db, config, api, feed, logging.getLogger("report"), mode)
                    e.stopping = True
                    print(json.dumps(report(e), ensure_ascii=False, indent=2))
                finally:
                    db.close()
                    asyncio.run(api.close())
        elif args.command == "replay":
            from bot.replay import replay
            asyncio.run(replay(args, config))
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        # 不输出可能含认证头、签名或私钥的 SDK/HTTP 异常原文。
        safe = str(exc) if isinstance(exc, (ValueError, FileNotFoundError, RuntimeError)) else type(exc).__name__
        print("操作未完成：" + safe, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
