from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from dataclasses import asdict, is_dataclass
from types import SimpleNamespace
from .config import D
from .models import Book


def plain(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if is_dataclass(obj):
        return asdict(obj)
    return obj


class MarketFeed:
    """官方 SDK 负责协议订阅、心跳、重连；连接重建后等待完整 book 快照。"""
    def __init__(self, client, emit, immediate=False):
        self.client, self.emit = client, emit
        self.immediate = immediate
        self.books = {}
        self.specs = {}
        self.tokens = set()
        self.task = None
        self.seen = OrderedDict()
        self.connected = False
        self.last_event = 0.0

    async def set_tokens(self, tokens):
        tokens = set(tokens)
        if tokens == self.tokens and self.task and not self.task.done():
            return
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.tokens = tokens
        self.books.clear()
        self.connected = False
        self.task = asyncio.create_task(self.run()) if tokens else None

    async def run(self):
        from polymarket.streams import MarketSpec
        delay = 1
        while True:
            try:
                async with await self.client.subscribe(MarketSpec(asset_ids=sorted(self.tokens), custom_feature_enabled=True)) as stream:
                    self.connected = True
                    self.books.clear()
                    await self.emit("feed_connected", {"tokens": len(self.tokens)})
                    async for event in stream:
                        received = time.time()
                        self.last_event = received
                        # SDK 自动重连/丢帧提示；丢帧时弃用增量，外层重订阅获取完整快照。
                        if getattr(event, "dropped", 0):
                            raise ConnectionError("stream dropped messages")
                        payload = plain(event.payload)
                        kind = event.type
                        stamp = getattr(event.payload, "timestamp", None)
                        ts = stamp.timestamp() if hasattr(stamp, "timestamp") else received
                        if ts > received + 10:
                            raise ValueError("future market timestamp")
                        key = json.dumps([kind, payload], sort_keys=True, default=str)
                        if kind not in {"book"} and key in self.seen:
                            continue
                        self.seen[key] = None
                        if len(self.seen) > 5000:
                            self.seen.popitem(last=False)
                        message = {"type": kind, "payload": payload, "timestamp": ts, "received_at": received}
                        # 在接收任务更新盘口，不等待交易主循环中的网络请求。
                        if self.immediate:
                            self.apply(message)
                            message["applied"] = True
                        await self.emit("market_message", message)
                        delay = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.books.clear()
                await self.emit("feed_disconnected", {"error_type": type(exc).__name__})
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    def apply(self, message):
        p, kind, received, ts = message["payload"], message["type"], message["received_at"], message["timestamp"]
        if kind == "book":
            token = str(p.get("asset_id", p.get("token_id", "")))
            old = self.books.get(token)
            if old and ts < old.timestamp:
                return
            raw = {**p, "asset_id": token, "timestamp": ts * 1000}
            for key, value in self.specs.get(token, {}).items():
                if raw.get(key) is None:
                    raw[key] = value
            self.books[token] = Book.parse(raw, received)
        elif kind == "price_change":
            for change in p.get("price_changes", []):
                token = str(change.get("asset_id", change.get("token_id", "")))
                book = self.books.get(token)
                if not book or ts < book.timestamp:
                    continue
                side = book.bids if change["side"] == "BUY" else book.asks
                price, size = D(change["price"]), D(change["size"])
                if size == 0:
                    side.pop(price, None)
                else:
                    side[price] = size
                book.received_at, book.timestamp = received, ts
        elif kind == "tick_size_change":
            book = self.books.get(str(p.get("asset_id", "")))
            if book:
                book.tick = str(p["new_tick_size"])
                book.received_at = received

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


async def user_feed(client, queue):
    from polymarket.streams import UserSpec
    delay = 1
    while True:
        try:
            async with await client.subscribe(UserSpec()) as stream:
                await queue.put(("user_connected", {}))
                async for update in stream:
                    # 不把包含 owner/API key 的原始事件写日志或行情文件。
                    await queue.put(("user_update", SimpleNamespace(type=update.type, payload=update.payload, received_at=time.time())))
                    delay = 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await queue.put(("user_disconnected", {"error_type": type(exc).__name__}))
            await asyncio.sleep(delay)
            delay = min(30, delay * 2)
