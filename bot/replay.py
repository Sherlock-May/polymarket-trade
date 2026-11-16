"""只回放已记录的公开行情；不使用历史真实/模拟成交作为回放成交。"""
import json
import logging
import sqlite3
import time
from pathlib import Path
from .config import Config, Rule
from .database import Database
from .engine import Engine
from .lock import ProcessLock
from .models import Market
from .polymarket_client import PaperBroker
from .reporting import report
from .websocket_client import MarketFeed


async def replay(args, config):
    if Path(args.output_db).exists():
        raise ValueError("回放输出数据库已存在，请指定新的 --output-db")
    source = sqlite3.connect(Path(args.source_db).resolve().as_uri() + "?mode=ro", uri=True)
    rounds = [json.loads(row[0]) for row in source.execute("SELECT data FROM market_rounds")]
    source.close()
    if any(r["mode"] != "paper" for r in rounds):
        raise ValueError("仅支持以模拟轮次数据库作为回放市场元数据")
    class Clock:
        offset = 0
        clock_uncertainty = 0
        clock_checked = time.time()
        value = 0
        def now(self):
            return self.value
    clock = Clock()
    feed = MarketFeed(None, None)
    feed.connected = True
    with ProcessLock(args.output_db):
        db = Database(args.output_db)
        try:
            rules = {r["rule"]["id"]: Rule(**r["rule"]) for r in rounds}
            config = Config(config.settings, list(rules.values()))
            e = Engine(db, config, clock, feed, logging.getLogger("replay"), "paper")
            e.report_directory = Path("reports/replay") / Path(args.output_db).stem
            e.broker = PaperBroker(e)
            for r in rounds:
                e.add_round(Market(**r["market"]), Rule(**r["rule"]))
            count = 0
            with Path(args.recording).open(encoding="utf-8") as recording:
                for line in recording:
                    msg = json.loads(line)
                    clock.value = msg["received_at"]
                    clock.clock_checked = time.time()
                    msg["received_at"] = time.time()
                    await e.on_message("market_message", msg)
                    await e.reconcile()
                    await e.manage()
                    count += 1
            e.stopping = True
            await e.manage()
            clock.value += config.settings["paper_latency_ms"] / 1000 + 1
            await e.reconcile()
            await e.manage()
            e.save()
            stats = report(e)
            print(json.dumps({"replayed_messages": count, "output_database": args.output_db, "stats": stats}, ensure_ascii=False, indent=2))
        finally:
            db.close()
