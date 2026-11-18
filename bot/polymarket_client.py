from __future__ import annotations

import asyncio
import os
import time
from dataclasses import asdict
from .config import D
from .models import Fill
from .pnl_calculator import estimate_fee


ACTIVE = {"INTENT", "UNKNOWN", "LIVE", "PARTIAL", "MATCHED", "CANCEL_PENDING"}
TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}


class OrderUnavailableError(RuntimeError):
    pass


class PaperBroker:
    def __init__(self, engine):
        self.engine = engine
        self.client = None
        self.pending = []
        self.trade_seq = 0
        self.depth_used = {}

    async def submit(self, order, market, deadline=None):
        book = self.engine.feed.books[order["asset_id"]]
        side = book.bids if order["side"] == "BUY" else book.asks
        # 可观察队列 + 保守乘数；撤单不被当成前方队列必然减少。
        order["queue_ahead"] = str(side.get(D(order["price"]), D(0)) * D(self.engine.config.settings["paper_queue_multiplier"]))
        order["ready_at"] = self.engine.api.now() + self.engine.config.settings["paper_latency_ms"] / 1000
        order["immediate_checked"] = False
        return {"exchange_id": "paper-" + order["local_id"], "status": "LIVE"}

    async def cancel(self, order):
        if not order.get("exchange_id"):
            return False
        order.setdefault("cancel_ready_at", self.engine.api.now() + self.engine.config.settings["paper_latency_ms"] / 1000)
        return self.engine.api.now() >= order["cancel_ready_at"]

    async def available(self, token=None, version="v1"):
        if token:
            return self.engine.total_shares(token), True
        return self.engine.paper_cash(), True

    def _fill(self, o, price, size, liquidity):
        if size <= 0:
            return
        market = self.engine.market(o["slug"])
        fee = estimate_fee(market, price, size, liquidity)
        self.trade_seq += 1
        ident = f"{o['local_id']}:{o.get('matched', '0')}:{self.trade_seq}:{self.engine.api.now()}"
        self.pending.append(asdict(Fill(ident, o["local_id"], str(price), str(size), str(fee), "estimated",
                                       self.engine.api.now(), liquidity)))
        o["matched"] = str(D(o.get("matched", "0")) + size)
        o["status"] = "FILLED" if D(o["matched"]) >= D(o["size"]) else "PARTIAL"

    async def reconcile(self):
        for o in list(self.engine.orders.values()):
            if o["status"] == "CANCEL_PENDING" and o.get("exchange_id") and self.engine.api.now() >= o.get("cancel_ready_at", float("inf")):
                o["status"] = "CANCELED"
            if o["status"] not in {"LIVE", "PARTIAL", "CANCEL_PENDING"} or self.engine.api.now() < o.get("ready_at", 0):
                continue
            if o.get("immediate_checked"):
                continue
            book = self.engine.feed.books.get(o["asset_id"])
            if not book or time.time() - book.received_at > self.engine.config.settings["book_max_age_seconds"]:
                continue
            o["immediate_checked"] = True
            depth = book.asks if o["side"] == "BUY" else book.bids
            for price, size in sorted(depth.items(), reverse=o["side"] == "SELL"):
                if (o["side"] == "BUY" and price > D(o["price"])) or (o["side"] == "SELL" and price < D(o["price"])):
                    continue
                key = (o["asset_id"], book.timestamp, o["side"], str(price))
                available = max(D(0), size - self.depth_used.get(key, D(0)))
                fill_size = min(D(o["size"]) - D(o.get("matched", "0")), available)
                self._fill(o, price, fill_size, "TAKER")
                self.depth_used[key] = self.depth_used.get(key, D(0)) + fill_size
                if o["status"] == "FILLED":
                    break
        result, self.pending = self.pending, []
        return result

    def on_trade(self, message):
        p = message["payload"]
        token, price, remaining = str(p["asset_id"]), D(p["price"]), D(p.get("size") or "0")
        for o in list(self.engine.orders.values()):
            if o["asset_id"] != token or o["status"] not in {"LIVE", "PARTIAL", "CANCEL_PENDING"} or not o.get("immediate_checked"):
                continue
            if o.get("cancel_ready_at") is not None and self.engine.api.now() >= o["cancel_ready_at"]:
                continue
            if self.engine.api.now() < o.get("ready_at", 0) or message["timestamp"] < o.get("ready_at", 0):
                continue
            # 只用真实成交量推进队列。触及报价/盘口数量下降不算成交。
            eligible = (o["side"] == "BUY" and p["side"] == "SELL" and price <= D(o["price"])) or (o["side"] == "SELL" and p["side"] == "BUY" and price >= D(o["price"]))
            if not eligible:
                continue
            ahead = D(o.get("queue_ahead", "0"))
            consumed = min(ahead, remaining)
            o["queue_ahead"] = str(ahead - consumed)
            remaining -= consumed
            size = min(remaining, D(o["size"]) - D(o.get("matched", "0")))
            self._fill(o, D(o["price"]), size, "MAKER")
            remaining -= size

    async def close(self):
        pass


class LiveBroker:
    def __init__(self, engine, client):
        self.engine, self.client = engine, client

    @classmethod
    async def create(cls, engine):
        from polymarket import AsyncSecureClient
        from polymarket.auth import RelayerApiKey
        kwargs = {"private_key": os.environ["POLYMARKET_PRIVATE_KEY"], "wallet": os.environ["POLYMARKET_WALLET_ADDRESS"]}
        if os.getenv("POLYMARKET_RELAYER_API_KEY"):
            kwargs["api_key"] = RelayerApiKey(key=os.environ["POLYMARKET_RELAYER_API_KEY"], address=os.environ["POLYMARKET_RELAYER_API_KEY_ADDRESS"])
        return cls(engine, await AsyncSecureClient.create(**kwargs))

    async def available(self, token=None, version="v1"):
        asset_type = "COLLATERAL" if token is None else ("CONDITIONAL-V2" if version == "v2" else "CONDITIONAL")
        balance = await self.client.get_balance_allowance(asset_type=asset_type, asset_id=token)
        # SDK 返回原始 6 位 base units；不自动发送授权交易。
        amount = D(balance.balance) / D(1_000_000)
        allowed = bool(balance.allowances) and all(D(v) >= D(balance.balance) for v in balance.allowances.values())
        if token is None:
            self.engine.last_cash_balance = str(amount)
            self.engine.last_cash_checked_at = time.time()
        return amount, allowed

    async def submit(self, order, market, deadline=None):
        # create + post 分离：post_order 不进入 SDK place_limit_order 的自动授权恢复流程。
        signed = await self.client.create_limit_order(asset_id=order["asset_id"], side=order["side"],
                                                       price=D(order["price"]), size=D(order["size"]),
                                                       expiration=int(deadline + 60) if deadline else None)
        if order["side"] == "BUY" and (self.engine.api.now() >= market.start or self.engine.stopping):
            return {"status": "REJECTED", "reason": "签名完成时已开盘或正在停机"}
        # 请求发出前先持久化 UNKNOWN；超时绝不重新签一笔订单。
        if deadline:
            order["expires_at"] = int(deadline + 60)
        order["status"] = "UNKNOWN"
        self.engine.save()
        response = await self.client.post_order(signed)
        if not response.ok:
            return {"status": "REJECTED", "reason": str(response.code)}
        return {"exchange_id": str(response.order_id), "status": "LIVE"}

    async def _get_order(self, ident):
        from polymarket.errors import UnexpectedResponseError
        from pydantic import ValidationError
        try:
            return await self.client.get_order(order_id=ident)
        except UnexpectedResponseError as exc:
            cause = exc.__cause__
            null_response = isinstance(cause, ValidationError) and any(
                e["loc"] == () and e["type"] == "model_type" and e.get("input") is None
                for e in cause.errors()
            )
            if not null_response:
                raise
            # 官方列表支持按 ID 查询终态；空结果不能直接当成撤单成功。
            matches = [x async for x in self.client.list_open_orders(id=ident).iter_items()]
            if len(matches) == 1 and str(matches[0].id) == ident:
                return matches[0]
            raise OrderUnavailableError("订单详情为空，需交叉核实到期、成交和持仓") from exc

    async def _expired_without_inventory(self, o, r, opened, trades):
        # 仅恢复已结束轮次的已知 GTD 入场单；未知提交和卖单继续人工核实。
        if (o["side"] != "BUY" or o["purpose"] != "ENTRY" or not o.get("exchange_id")
                or o["status"] in {"INTENT", "UNKNOWN"} or D(o.get("matched", "0")) != 0
                or self.engine.api.now() <= max(r["market"]["end"], o.get("expires_at", int(r["deadline"] + 60))) + 60):
            return False
        if any(str(x.id) == o["exchange_id"] for x in opened):
            return False
        if any(str(t.taker_order_id) == o["exchange_id"] or any(str(m.order_id) == o["exchange_id"] for m in t.maker_orders) for t in trades):
            return False
        if any(f["order_id"] == o["local_id"] for f in self.engine.fills):
            return False
        balance, _ = await self.available(o["asset_id"], r["market"].get("version", "v1"))
        if balance != 0:
            return False
        o["status"] = "EXPIRED"
        o["status_source"] = "gtd_expiry_cross_checked"
        o["expiry_checked_at"] = self.engine.api.now()
        self.engine.emit("旧买单到期已核实", order=o["local_id"], basis="轮次及GTD已到期、无挂单、无相关成交、持仓余额为零")
        return True

    async def cancel(self, order):
        if not order.get("exchange_id"):
            return False
        response = await self.client.cancel_order(order_id=order["exchange_id"])
        acknowledged = order["exchange_id"] in {str(x) for x in getattr(response, "canceled", [])}
        if acknowledged:
            order["cancel_acknowledged"] = True
            order["cancel_acknowledged_at"] = self.engine.api.now()
        # 取消响应不代替最终状态查询；成交仍由 trades 记录确认。
        try:
            final = await self._get_order(order["exchange_id"])
        except OrderUnavailableError:
            # 明确的撤单回执可证明剩余挂单已撤；不能据此推断没有成交。
            if order.get("cancel_acknowledged"):
                order["status_source"] = "cancel_acknowledged_detail_unavailable"
                order["trade_reconcile_required"] = True
                return True
            raise
        order["matched"] = str(final.size_matched)
        status = final.status.upper()
        if status == "EXPIRED":
            order["status"] = "EXPIRED"
        elif status in {"CANCELED", "CANCELLED"}:
            return True
        if final.size_matched >= final.original_size:
            order["status"] = "FILLED"
        return False

    def trade_fills(self, trade, local):
        if trade.status != "CONFIRMED" or getattr(trade, "matched_at", None) is None:
            return []
        known = {o.get("exchange_id"): o for o in local if o.get("exchange_id")}
        legs = []
        if str(trade.taker_order_id) in known:
            legs.append((known[str(trade.taker_order_id)], trade.price, trade.size, "TAKER"))
        for maker in (trade.maker_orders or []):
            if str(maker.order_id) in known:
                legs.append((known[str(maker.order_id)], maker.price, maker.matched_amount, "MAKER"))
        fills = []
        for o, price, size, liquidity in legs:
            fee = estimate_fee(self.engine.market(o['slug']), price, size, liquidity)
            fills.append(asdict(Fill(f"{trade.id}:{o['local_id']}", o['local_id'], str(price), str(size), str(fee),
                                    'actual' if liquidity == 'MAKER' else 'estimated', trade.matched_at.timestamp(),
                                    liquidity, tx_hash=str(trade.transaction_hash or ''))))
        return fills

    async def reconcile(self, archived=None):
        fills = []
        # 已结束的轮次独立处理；旧单详情不可用不能饿死当前成交查询。
        for r in list(self.engine.rounds.values()):
            if r["status"] == "FINAL":
                continue
            ended = self.engine.api.now() >= r["market"]["end"]
            if archived is not None and ended != archived:
                continue
            local = [o for o in list(self.engine.orders.values()) if o["slug"] == r["market"]["slug"]]
            if not local:
                continue
            try:
                async with asyncio.timeout(6 if ended else 10):
                    batch = await self._reconcile_round(r, local)
                fills.extend(batch)
                if self.engine.mode == "live":
                    await self.engine.accept_fills(batch)
            except Exception as exc:
                if not ended:
                    raise
                self.engine.limited("历史轮次核实失败", r["market"]["slug"], error_type=type(exc).__name__)
        return fills

    async def _reconcile_round(self, r, local):
        fills = []
        opened = [o async for o in self.client.list_open_orders(market=r["market"]["condition_id"]).iter_items()]
        trades = [t async for t in self.client.list_account_trades(market=r["market"]["condition_id"]).iter_items()]
        # 先交付撮合通知，不等待逐笔订单详情请求结束。
        for trade in trades:
            self.engine.observe_matched_trade(trade, local)
        for o in local:
            if o["status"] in {"INTENT", "UNKNOWN", "CANCEL_PENDING"} and not o.get("exchange_id"):
                # 不确定的提交只认领唯一匹配；没有证据时保持 UNKNOWN，禁止补发。
                candidates = [x for x in opened if str(x.asset_id) == o["asset_id"] and x.side == o["side"] and x.price == D(o["price"]) and x.original_size == D(o["size"]) and abs(x.created_at.timestamp() - o["created_at"]) < 120]
                if len(candidates) == 1:
                    o["exchange_id"], o["status"] = str(candidates[0].id), "LIVE"
                elif not candidates:
                    # 订单可能在响应丢失前已完全成交，因而不在 open_orders。
                    # 从同市场成交提取候选 ID，再查最终订单验证原始数量/方向/时间。
                    ids = set()
                    for t in trades:
                        matched_at = getattr(t, "matched_at", None)
                        if matched_at is None or abs(matched_at.timestamp() - o["created_at"]) >= 120:
                            continue
                        if str(getattr(t, "asset_id", "")) == o["asset_id"]:
                            ids.add(str(t.taker_order_id))
                        for maker in t.maker_orders:
                            if str(maker.asset_id) == o["asset_id"]:
                                ids.add(str(maker.order_id))
                    owned = {x.get("exchange_id") for x in self.engine.orders.values() if x is not o}
                    verified = []
                    for ident in ids - owned:
                        try:
                            remote = await self._get_order(ident)
                        except Exception:
                            continue
                        if str(remote.asset_id) == o["asset_id"] and remote.side == o["side"] and remote.price == D(o["price"]) and remote.original_size == D(o["size"]) and abs(remote.created_at.timestamp() - o["created_at"]) < 120:
                            verified.append(remote)
                    if len(verified) == 1:
                        o["exchange_id"], o["status"] = str(verified[0].id), "LIVE"
            if o.get("exchange_id") and o["status"] in ACTIVE:
                try:
                    remote = await self._get_order(o["exchange_id"])
                except OrderUnavailableError:
                    if await self._expired_without_inventory(o, r, opened, trades):
                        continue
                    if (o["side"] == "SELL" and self.engine.api.now() > r["market"]["end"] + 60
                            and not any(str(x.id) == o["exchange_id"] for x in opened)):
                        raw = await self.engine.api.market(r["market"]["slug"])
                        if raw.get("closed") is True and raw.get("umaResolutionStatus") == "resolved":
                            o["status"] = "CANCELED"
                            o["status_source"] = "resolved_market_no_open_order"
                            o["trade_reconcile_required"] = True
                            self.engine.emit("旧止盈单已结束", order=o["local_id"], basis="市场已最终结算且挂单列表无此订单；成交单独核算")
                            continue
                    self.engine.limited("旧订单详情不可用继续核实", o["local_id"])
                    continue
                o["matched"] = str(remote.size_matched)
                if remote.size_matched >= remote.original_size:
                    o["status"] = "FILLED"
                elif remote.status.upper() in {"CANCELED", "CANCELLED"}:
                    o["status"] = "CANCELED"
                    self.engine.emit("撤单已确认", order=o["local_id"], matched=o.get("matched", "0"))
                elif remote.status.upper() == "EXPIRED":
                    o["status"] = "EXPIRED"
                elif remote.size_matched > 0:
                    o["status"] = "PARTIAL"
        for trade in trades:
            # 即使尚未 CONFIRMED，也立即阻止本轮新买单并撤销剩余买单。
            known = {o.get("exchange_id"): o for o in local if o.get("exchange_id")}
            legs = []
            if str(trade.taker_order_id) in known:
                legs.append((known[str(trade.taker_order_id)], trade.price, trade.size, "TAKER"))
            for maker in trade.maker_orders:
                if str(maker.order_id) in known:
                    legs.append((known[str(maker.order_id)], maker.price, maker.matched_amount, "MAKER"))
            for o, price, size, liquidity in legs:
                if trade.status not in {"FAILED"} and o["side"] == "BUY":
                    r["entry_closed"] = True
                if trade.status != "CONFIRMED":
                    continue
            fills.extend(self.trade_fills(trade, local))
        for o in local:
            if not o.get("trade_reconcile_required"):
                continue
            related = [t for t in trades if str(t.taker_order_id) == o.get("exchange_id")
                       or any(str(m.order_id) == o.get("exchange_id") for m in (t.maker_orders or []))]
            pending = any(t.status not in {"CONFIRMED", "FAILED"} for t in related)
            # 撤单回执后的成交可能稍迟可见；保留一轮宽限期，不提前结算盈亏。
            if not pending and self.engine.api.now() >= o.get("cancel_acknowledged_at", 0) + 60:
                o["trade_reconcile_required"] = False
        return fills

    async def close(self):
        await self.client.__aexit__(None, None, None)
