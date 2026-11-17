from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import ROUND_DOWN
from pathlib import Path
from .config import Config, Rule, D
from .database import dumps
from .logger import event, error_fields
from .models import Book, Market, utc_iso
from .pnl_calculator import position
from .polymarket_client import ACTIVE, TERMINAL, PaperBroker, LiveBroker


class Engine:
    def __init__(self, db, config, api, feed, log, mode="paper"):
        self.db, self.config, self.api, self.feed, self.log, self.mode = db, config, api, feed, log, mode
        self.rounds = {r["market"]["slug"]: r for r in db.rounds()}
        self.orders = {o["local_id"]: o for o in db.orders()}
        for order in self.orders.values():
            order.pop("last_cancel_attempt", None)  # monotonic 时间不能跨系统重启复用
        self.fills = db.fills()
        self.fill_ids = {f["id"] for f in self.fills}
        self.broker = None
        self.stopping = False
        self.stop_reason = "未记录原因"
        self.user_connected = mode == "paper"
        self.started_at = time.time()
        self.last_reconcile = 0
        self.last_status = 0
        self.last_settlement = 0
        self.last_geo = 0
        self.geo_allowed = mode == "paper"
        self.connection_reconciled = mode == "paper"
        self.queue = asyncio.Queue(maxsize=10000)
        self.user_queue = asyncio.Queue(maxsize=10000)
        self.reconcile_requested = False
        self.protect_locks = {}
        self.throttle = {}
        self.cancel_retries = {}
        self.recording = None
        modes = {r["mode"] for r in self.rounds.values()}
        if modes and modes != {mode}:
            raise ValueError("禁止共用模拟和实盘数据库，请更换 DATABASE_PATH")

    def emit(self, name, level="INFO", **data):
        data["mode"] = self.mode
        event(self.db, self.log, name, level, **data)

    def limited(self, name, key, interval=60, **data):
        if time.monotonic() - self.throttle.get((name, key), -1e9) >= interval:
            self.throttle[(name, key)] = time.monotonic()
            self.emit(name, key=key, **data)

    def market(self, slug):
        return Market(**self.rounds[slug]["market"])

    def save(self):
        with self.db.transaction():
            for r in self.rounds.values():
                self.db.put_round(r)
            for o in self.orders.values():
                self.db.put_order(o)
            for f in self.fills:
                self.db.insert_fill(f)
            for r in self.rounds.values():
                for token in r["market"]["tokens"].values():
                    p = self.position(r["market"]["slug"], token)
                    self.db.conn.execute("INSERT INTO positions VALUES(?,?,?) ON CONFLICT(slug,asset_id) DO UPDATE SET data=excluded.data", (r["market"]["slug"], token, dumps(p)))

    def add_round(self, market, rule):
        if market.slug in self.rounds:
            return
        self.rounds[market.slug] = {"market": asdict(market), "rule": asdict(rule), "mode": self.mode,
                                   "deadline": market.start + rule.entry_window_seconds,
                                   "status": "WAITING", "entry_closed": False, "entry_attempted": False,
                                   "skip_reason": "", "settlement": None, "redeem": None,
                                   "opened_logged": False, "deadline_logged": False}
        self.save()
        self.emit("发现下一轮市场", slug=market.slug, start=utc_iso(market.start), end=utc_iso(market.end),
                  deadline=utc_iso(market.start + rule.entry_window_seconds), rule=rule.id)

    def position(self, slug, token):
        p = position(self.fills, self.orders, slug, token)
        settlement = self.rounds[slug].get("settlement")
        if settlement:
            payout = D(p["shares"]) * D(settlement["payouts"].get(token, "0"))
            p["settlement_receivable"] = str(payout)
            p["settlement_pnl"] = str(payout - D(p["cost"]))
        return p

    def total_shares(self, token):
        return sum((D(self.position(slug, token)["shares"]) for slug, r in self.rounds.items() if token in r["market"]["tokens"].values()), D(0))

    def paper_cash(self):
        cash = D(self.config.settings["paper_initial_balance"])
        for f in self.fills:
            o = self.orders[f["order_id"]]
            cash += (D(f["price"]) * D(f["shares"]) * (-1 if o["side"] == "BUY" else 1)) - D(f["fee"])
        for r in self.rounds.values():
            if r.get("settlement"):
                for token in r["market"]["tokens"].values():
                    cash += D(self.position(r["market"]["slug"], token)["settlement_receivable"])
        return cash

    def reserved(self, side="BUY", token=None):
        if side == "SELL":
            amount = D(0)
            for o in self.orders.values():
                if o["side"] != "SELL" or (token and o["asset_id"] != token):
                    continue
                confirmed = sum((D(f["shares"]) for f in self.fills if f["order_id"] == o["local_id"]), D(0))
                # 撮合但尚未链上确认的卖出仍占用库存，包括已 FILLED/CANCELED 的订单。
                target = D(o["size"]) if o["status"] in ACTIVE else D(o.get("matched", "0"))
                amount += max(D(0), target - confirmed)
            return amount
        return sum(((D(o["size"]) - D(o.get("matched", "0"))) * (D(o["price"]) if side == "BUY" else 1)
                    for o in self.orders.values() if o["side"] == side and o["status"] in ACTIVE and (token is None or token == o["asset_id"])), D(0))

    def pending_inventory(self, token=None):
        # 已撮合未链上确认的份额也占用风险预算，不把撤单后的 pending 当成零风险。
        total = D(0)
        for o in self.orders.values():
            if o["side"] != "BUY" or (token and o["asset_id"] != token):
                continue
            confirmed = sum((D(f["shares"]) for f in self.fills if f["order_id"] == o["local_id"]), D(0))
            total += max(D(0), D(o.get("matched", "0")) - confirmed) * D(o["price"])
        return total

    def exposure(self):
        cost = sum((D(self.position(slug, token)["cost"]) for slug, r in self.rounds.items() if not r.get("settlement") for token in r["market"]["tokens"].values()), D(0))
        return cost + self.reserved() + self.pending_inventory()

    def daily_loss(self):
        # UTC 日界；已实现亏损与当前库存成本均保守计入暂停条件，不触发止损。
        today = datetime.fromtimestamp(self.api.now(), timezone.utc).date()
        net = D(0)
        inventory = {}
        for f in sorted(self.fills, key=lambda x: (x["timestamp"], x["id"])):
            o = self.orders[f["order_id"]]
            key = (o["slug"], o["asset_id"])
            qty, cost = inventory.get(key, (D(0), D(0)))
            size, price, fee = D(f["shares"]), D(f["price"]), D(f["fee"])
            if o["side"] == "BUY":
                qty, cost = qty + size, cost + size * price + fee
            elif qty:
                basis = cost * size / qty
                if datetime.fromtimestamp(f["timestamp"], timezone.utc).date() == today:
                    net += size * price - fee - basis
                qty, cost = qty - size, cost - basis
            inventory[key] = (qty, cost)
        for slug, r in self.rounds.items():
            for token in r["market"]["tokens"].values():
                p = self.position(slug, token)
                if r.get("settlement"):
                    if datetime.fromtimestamp(r["settlement"]["confirmed_at"], timezone.utc).date() == today:
                        net += D(p.get("settlement_pnl", "0"))
                else:
                    net -= D(p["cost"])
        return max(D(0), -net)

    async def accept_fills(self, fills):
        protect_rounds = set()
        for f in fills:
            if f["id"] in self.fill_ids:
                continue
            o = self.orders[f["order_id"]]
            if (o["side"] == "BUY" and D(f["price"]) > D(o["price"])) or (o["side"] == "SELL" and D(f["price"]) < D(o["price"])):
                raise ValueError("成交价格越过限价，停止新增订单并要求对账")
            self.fill_ids.add(f["id"])
            self.fills.append(f)
            if o["side"] == "BUY":
                self.rounds[o["slug"]]["entry_closed"] = True
                protect_rounds.add(o["slug"])
            confirmed = sum((D(x["shares"]) for x in self.fills if x["order_id"] == o["local_id"]), D(0))
            o["matched"] = str(max(confirmed, D(o.get("matched", "0"))))
            if confirmed >= D(o["size"]):
                o["status"] = "FILLED"
            self.save()
            self.emit("真实成交" if self.mode == "live" else "模拟成交", slug=o["slug"], side=o["side"], asset=o["asset_id"],
                      order=o['local_id'], matched_at=f['timestamp'], price=f["price"], shares=f["shares"], fee=f["fee"], fee_kind=f["fee_kind"], fill_id=f["id"])
        if self.mode == "live":
            # 确认成交后先保护库存，不等待队列中下一次全账户对账。
            for slug in sorted(protect_rounds):
                await self.protect(self.rounds[slug])

    async def reconcile(self):
        try:
            async with asyncio.timeout(15):
                fills = await self.broker.reconcile(archived=False) if isinstance(self.broker, LiveBroker) else await self.broker.reconcile()
            await self.accept_fills(fills)
            for o in self.orders.values():
                if o["side"] == "BUY" and D(o.get("matched", "0")) > 0:
                    self.rounds[o["slug"]]["entry_closed"] = True
            unresolved = any(o["status"] in {"INTENT", "UNKNOWN", "CANCEL_PENDING"} and not o.get("exchange_id") for o in self.orders.values())
            self.connection_reconciled = True
            if unresolved:
                self.limited("旧订单等待核实", "account")
            self.last_reconcile = self.api.now()
            self.save()
        except Exception as exc:
            self.connection_reconciled = False
            self.limited("对账失败暂停新开仓", "account", **error_fields(exc))
        finally:
            # 失败也遵守对账间隔，避免每次主循环立即重试。
            self.last_reconcile = self.api.now()

    async def cancel(self, o, reason):
        if o["status"] not in ACTIVE:
            return
        if self.mode == "live" and not o.get("exchange_id"):
            o["status"] = "UNKNOWN"
            self.save()
            self.limited("未知订单等待核实", o["local_id"])
            return
        attempts, retry_at = self.cancel_retries.get(o["local_id"], (0, 0))
        if time.monotonic() < retry_at:
            return
        attempts += 1
        self.cancel_retries[o["local_id"]] = (attempts, time.monotonic() + min(60, 2 ** min(attempts, 6)))
        o["status"] = "CANCEL_PENDING"
        self.save()
        self.emit("请求撤单", order=o["local_id"], reason=reason)
        try:
            async with asyncio.timeout(6):
                done = await self.broker.cancel(o)
            if done:
                o["status"] = "CANCELED"
                self.emit("撤单已确认", order=o["local_id"], matched=o.get("matched", "0"))
            else:
                self.limited("撤单尚未确认", o["local_id"], status=o["status"])
        except Exception as exc:
            self.limited("撤单异常需继续对账", o["local_id"], **error_fields(exc))
        if o["status"] not in ACTIVE:
            self.cancel_retries.pop(o["local_id"], None)
        self.save()

    async def submit(self, r, token, side, price, size, purpose):
        m = self.market(r["market"]["slug"])
        m.validate_order(price, size)
        o = {"local_id": uuid.uuid4().hex, "slug": m.slug, "asset_id": token, "side": side,
             "purpose": purpose, "price": str(price), "size": str(size), "status": "INTENT",
             "matched": "0", "created_at": self.api.now(), "exchange_id": None}
        self.orders[o["local_id"]] = o
        self.save()  # 先保存意图，再发网络请求
        self.emit("提交订单", slug=m.slug, purpose=purpose, side=side, price=str(price), shares=str(size), order=o["local_id"])
        try:
            async with asyncio.timeout(12):
                result = await self.broker.submit(o, m, r["deadline"] if side == "BUY" and self.mode == "live" else None)
            o.update(result)
            self.emit("订单被接受" if o["status"] == "LIVE" else "订单被拒绝", order=o["local_id"], status=o["status"], reason=o.get("reason", ""))
        except asyncio.CancelledError:
            # 停机取消后台成交任务时，不能留下永远占用库存的未发送 INTENT。
            o["status"] = "REJECTED" if self.mode == "live" and o["status"] == "INTENT" else "UNKNOWN"
            self.save()
            self.emit("订单未发送" if o["status"] == "REJECTED" else "提交结果未知禁止重复下单", "ERROR",
                      order=o["local_id"], error_type="CancelledError")
            raise
        except Exception as exc:
            if self.mode == "live" and o["status"] == "INTENT":
                o["status"] = "REJECTED"
                self.emit("订单未发送", "ERROR", order=o["local_id"], error_type=type(exc).__name__)
            else:
                o["status"] = "UNKNOWN"
                self.emit("提交结果未知禁止重复下单", "ERROR", order=o["local_id"], error_type=type(exc).__name__)
        self.save()
        return o

    async def open_round(self, r):
        m, rule = self.market(r["market"]["slug"]), Rule(**r["rule"])
        if self.stopping or r["entry_attempted"] or r["entry_closed"]:
            return
        now = self.api.now()
        # 只给未来一轮预挂；中途启动不参与当前盘。
        if now >= m.start:
            r["entry_closed"], r["skip_reason"] = True, "错过开盘前预挂窗口"
            return
        if now < m.start - rule.preopen_seconds:
            return
        try:
            s = self.config.settings
            if abs(self.api.offset) + self.api.clock_uncertainty > s["max_clock_skew_seconds"] or time.time() - self.api.clock_checked > 120:
                raise ValueError("时钟未同步或偏差过大")
            if not self.geo_allowed or not self.user_connected or not self.connection_reconciled:
                raise ValueError("地区检查、用户连接或账户对账未就绪")
            if not self.feed.connected:
                raise ValueError("市场 WebSocket 未连接")
            for token in m.tokens.values():
                b = self.feed.books.get(token)
                if self.mode == "live" and (not b or not b.synced or time.time() - b.received_at > s["book_max_age_seconds"]):
                    # 静默盘口可能没有推送；主动取完整快照，不能只放宽过期阈值。
                    async with asyncio.timeout(4):
                        raw = await self.api.book(token)
                    b = Book.parse(raw, time.time())
                    if (b.asset_id != token or not raw.get("tick_size") or not raw.get("min_order_size")
                            or not isinstance(raw.get("bids"), list) or not isinstance(raw.get("asks"), list)):
                        raise ValueError("盘口快照资产或交易规格不完整")
                    current = self.feed.books.get(token)
                    if current and current.timestamp > b.timestamp:
                        b = current
                    else:
                        self.feed.books[token] = b
                if not b or not b.synced or time.time() - b.received_at > s["book_max_age_seconds"]:
                    raise ValueError("等待新鲜完整盘口")
                m.tick, m.min_size = b.tick, b.min_size
                m.validate_order(rule.entry_price, rule.shares)
                m.validate_order(rule.take_profit_price, rule.shares)
            if not m.fee_known:
                raise ValueError("未知费率，禁止盲目估算成本")
            budget = 2 * D(rule.entry_price) * D(rule.shares)
            fee_buffer = budget * D(m.fee_rate)  # 仅作资金预留；最终费用按曲线/实际回报统计
            required = budget + fee_buffer
            if self.exposure() + required > D(s["max_open_exposure"]):
                raise ValueError("超出总敞口上限")
            if self.daily_loss() >= D(s["max_daily_loss"]):
                raise ValueError("达到当日亏损上限")
            balance, allowed = await self.broker.available()
            if not allowed or balance - self.reserved() < required:
                raise ValueError("余额或已有授权额度不足")
            if self.mode == "live":
                fresh = await self.api.market(m.slug)
                if fresh.get("acceptingOrders") is not True or fresh.get("closed") is not False:
                    raise ValueError("提交前复查市场已不接受订单")
        except Exception as exc:
            reason = str(exc) if isinstance(exc, (ValueError, KeyError)) else type(exc).__name__
            r["skip_reason"] = reason
            self.limited("暂缓预挂", m.slug, reason=reason)
            return
        r["market"] = asdict(m)
        r["entry_attempted"], r["status"], r["skip_reason"] = True, "ACTIVE", ""
        self.save()
        # 同一事件循环串行创建两笔；若第一笔已经撮合，不再追加第二笔。
        for outcome in ("UP", "DOWN"):
            if r["entry_closed"] or self.api.now() >= m.start or self.stopping:
                break
            o = await self.submit(r, m.tokens[outcome], "BUY", rule.entry_price, rule.shares, "ENTRY")
            await self.reconcile()
            if o["status"] in {"UNKNOWN", "REJECTED"}:
                r["entry_closed"] = True
                break

    async def protect(self, r):
        lock = self.protect_locks.setdefault(r['market']['slug'], asyncio.Lock())
        async with lock:
            await self._protect(r)

    async def _protect(self, r):
        m, rule = self.market(r["market"]["slug"]), Rule(**r["rule"])
        if self.api.now() >= m.end or r.get("settlement"):
            return
        for token in m.tokens.values():
            own = D(self.position(m.slug, token)["shares"])
            reserved = self.reserved("SELL", token)
            qty = max(D(0), own - reserved)
            if qty <= 0:
                continue
            recent = [o for o in self.orders.values() if o["slug"] == m.slug and o["asset_id"] == token and o["side"] == "SELL"]
            rejected = [o for o in recent if o["status"] == "REJECTED"]
            if len(rejected) >= 5 or (rejected and self.api.now() - rejected[-1]["created_at"] < min(60, 2 ** len(rejected))):
                self.limited("止盈重试暂停需检查", token, slug=m.slug, rejected=len(rejected))
                continue
            try:
                started = time.monotonic()
                self.limited("止盈处理中", token, slug=m.slug, asset=token, shares=str(qty))
                async with asyncio.timeout(6):
                    balance, allowed = await self.broker.available(token, m.version)
                qty = min(qty, max(D(0), balance - reserved)).quantize(D("0.01"), rounding=ROUND_DOWN)
                b = self.feed.books.get(token)
                if self.mode == "paper" and not b:
                    continue
                if b:
                    m.tick, m.min_size = b.tick, b.min_size
                if not allowed or qty < D(m.min_size):
                    self.limited("持仓暂不可挂止盈", token, slug=m.slug, shares=str(qty), balance=str(balance),
                                 min_size=m.min_size, allowance_ok=allowed)
                    continue
                m.validate_order(rule.take_profit_price, qty)
                r["market"] = asdict(m)
                await self.submit(r, token, "SELL", rule.take_profit_price, qty, "TAKE_PROFIT")
                self.emit("止盈处理耗时", slug=m.slug, asset=token, milliseconds=round((time.monotonic() - started) * 1000))
            except Exception as exc:
                self.limited("止盈保护异常", token, slug=m.slug, **error_fields(exc))

    async def manage(self):
        rules = {r.id: r for r in self.config.markets}
        for r in self.rounds.values():
            m, now = r["market"], self.api.now()
            if r["status"] == "FINAL":
                continue
            if self.mode == "live" and now >= m["end"]:
                # 历史轮次由独立任务对账、撤单及结算，不占用当前轮次下单路径。
                r["entry_closed"] = True
                r["status"] = "WAIT_SETTLEMENT"
                continue
            enabled = rules.get(r["rule"]["id"])
            if enabled is None or not enabled.enabled or self.stopping:
                r["entry_closed"] = True
                r["skip_reason"] = "配置关闭或安全停机"
            if now >= m["start"] and not r["opened_logged"]:
                r["opened_logged"] = True
                self.emit("新一轮已开盘", slug=m["slug"], deadline=utc_iso(r["deadline"]))
            if now >= r["deadline"] - self.config.settings["cancel_before_deadline_seconds"]:
                r["entry_closed"] = True
                if not r["deadline_logged"]:
                    r["deadline_logged"] = True
                    self.emit("开仓窗口关闭", slug=m["slug"])
            # 先对可卖的已确认份额挂止盈，再撤销剩余买单；不等待慢撤单才保护持仓。
            await self.protect(r)
            if r["entry_closed"] or now >= m["end"]:
                for o in list(self.orders.values()):
                    if o["slug"] == m["slug"] and o["status"] in ACTIVE and (o["side"] == "BUY" or now >= m["end"]):
                        await self.cancel(o, "本轮已成交/开仓到期/配置关闭/市场结束")
            if now >= m["end"]:
                r["status"] = "WAIT_SETTLEMENT"
            elif enabled and enabled.enabled:
                await self.open_round(r)
        self.save()

    async def settle(self):
        import json
        for slug, r in list(self.rounds.items()):
            if self.api.now() < r["market"]["end"] or r["status"] == "FINAL":
                continue
            local = [o for o in self.orders.values() if o["slug"] == slug]
            if any(o["status"] in ACTIVE for o in local):
                continue
            if any(o.get("trade_reconcile_required") for o in local):
                continue
            if any(self.pending_inventory(token) > 0 for token in r["market"]["tokens"].values()):
                continue
            remaining = sum((D(self.position(slug, t)["shares"]) for t in r["market"]["tokens"].values()), D(0))
            if remaining == 0:
                r["status"] = "FINAL"
                self.emit("本轮结束", slug=slug, result="已止盈" if local and any(o["side"] == "SELL" for o in local) else "无成交", skip_reason=r["skip_reason"])
                continue
            raw = await self.api.market(slug)
            if raw.get("umaResolutionStatus") != "resolved" or raw.get("closed") is not True:
                self.limited("等待最终结算", slug)
                continue
            prices = raw.get("outcomePrices")
            prices = json.loads(prices) if isinstance(prices, str) else prices
            labels = raw.get("outcomes")
            labels = json.loads(labels) if isinstance(labels, str) else labels
            if not prices or len(prices) != 2 or sum(map(D, prices)) != 1:
                self.limited("结算数据待核实", slug)
                continue
            payouts = {r["market"]["tokens"][label.upper()]: str(price) for label, price in zip(labels, prices)}
            if r.get("settlement") is None:
                r["settlement"] = {"payouts": payouts, "confirmed_at": self.api.now()}
                self.save()
                self.emit("最终结算已确认", slug=slug, payouts=payouts)
            if self.mode == "paper":
                r["redeem"] = {"status": "SIMULATED"}
                r["status"] = "FINAL"
            elif sum((D(self.position(slug, token)["shares"]) * D(payouts[token]) for token in payouts), D(0)) == 0:
                r["status"] = "FINAL"
                self.emit("亏损轮次已结算", slug=slug)
            elif os.getenv("AUTO_REDEEM", "false").lower() == "true" and r.get("redeem") is None:
                # 保存意图，未知提交不自动重复赎回。
                r["redeem"] = {"status": "SUBMITTING", "tx_hash": "", "gas_fee": None}
                self.save()
                try:
                    tx = await self.broker.client.redeem_positions(condition_id=r["market"]["condition_id"])
                    r["redeem"]["transaction_id"] = getattr(tx, "transaction_id", None)
                    r["redeem"]["tx_hash"] = str(getattr(tx, "transaction_hash", "") or "")
                    r["redeem"]["status"] = "SUBMITTED"
                    self.save()
                    self.emit("赎回请求已提交等待确认", slug=slug, tx_hash=r["redeem"]["tx_hash"])
                except Exception as exc:
                    r["redeem"]["status"] = "UNKNOWN"
                    self.emit("赎回结果需核实", "ERROR", slug=slug, error_type=type(exc).__name__)
            if self.mode == "live" and r.get("redeem") and r["redeem"]["status"] in {"SUBMITTED", "UNKNOWN", "SUBMITTING"}:
                redemption = r["redeem"]
                if redemption.get("transaction_id"):
                    state = await self.api.redemption_status(redemption["transaction_id"])
                    if state.get("tx_hash"):
                        redemption["tx_hash"] = state["tx_hash"]
                    if state["state"] == "STATE_CONFIRMED":
                        redemption["status"], r["status"] = "CONFIRMED", "FINAL"
                    elif state["state"] in {"STATE_FAILED", "STATE_INVALID"}:
                        redemption["status"] = "FAILED"
                receipt = await self.api.receipt(redemption.get("tx_hash", ""))
                if receipt:
                    if int(receipt.get("status", "0x0"), 16) == 1:
                        redemption["status"], r["status"] = "CONFIRMED", "FINAL"
                    else:
                        redemption["status"] = "FAILED"
                    if receipt.get("effectiveGasPrice") and receipt.get("gasUsed"):
                        redemption["chain_gas_cost_POL"] = str(D(int(receipt["gasUsed"], 16)) * D(int(receipt["effectiveGasPrice"], 16)) / D(10**18))
                        redemption["gas_paid_by_account"] = "unverified"  # Relayer 可能代付，不重复扣用户收益
                if redemption["status"] == "CONFIRMED":
                    self.emit("赎回已确认", slug=slug, tx_hash=redemption.get("tx_hash", ""))
            if self.mode == "live" and r["status"] != "FINAL":
                self.limited("待赎回或核实赎回状态", slug, redemption=r.get("redeem"))
        self.save()

    async def on_message(self, kind, data):
        if kind == "market_message":
            if data["type"] == "book":
                token = str(data["payload"].get("asset_id", ""))
                for r in self.rounds.values():
                    if token in r["market"]["tokens"].values():
                        for key, field in (("tick_size", "tick"), ("min_order_size", "min_size")):
                            if data["payload"].get(key) is None:
                                data["payload"][key] = r["market"][field]
                        break
            if not data.get("applied"):
                self.feed.apply(data)
            if self.recording:
                self.recording.write(dumps(data) + "\n")
                self.recording.flush()
            if isinstance(self.broker, PaperBroker) and data["type"] == "last_trade_price":
                self.broker.on_trade(data)
                await self.accept_fills(await self.broker.reconcile())
            if data["type"] == "tick_size_change":
                token, tick = str(data["payload"]["asset_id"]), D(data["payload"]["new_tick_size"])
                for r in self.rounds.values():
                    if token in r["market"]["tokens"].values():
                        r["market"]["tick"] = str(tick)
                for o in self.orders.values():
                    if o["asset_id"] == token and o["status"] in ACTIVE and D(o["price"]) % tick:
                        await self.cancel(o, "价格步长变更，原价格失效")
        elif kind == "discovered":
            if self.stopping:
                return
            rule, market = data
            latest = next((r for r in self.config.markets if r.id == rule.id and r.enabled), None)
            if latest and market and market.start > self.api.now():
                self.add_round(market, latest)
        elif kind == "user_update":
            p = data.payload
            if data.type == "trade" and p.status != "FAILED":
                ids = {str(p.taker_order_id)} | {str(x.order_id) for x in (p.maker_orders or [])}
                for o in self.orders.values():
                    if o.get("exchange_id") in ids and o["side"] == "BUY":
                        self.rounds[o["slug"]]["entry_closed"] = True
                if p.status == 'CONFIRMED' and hasattr(self.broker, 'trade_fills'):
                    await self.accept_fills(self.broker.trade_fills(p, list(self.orders.values())))
            elif data.type == "order" and p.size_matched > 0:
                for o in self.orders.values():
                    if o.get("exchange_id") == str(p.id):
                        o["matched"] = str(max(D(o.get("matched", "0")), p.size_matched))
                        if o["side"] == "BUY":
                            self.rounds[o["slug"]]["entry_closed"] = True
            self.reconcile_requested = True
        elif kind in {"user_connected", "user_disconnected"}:
            self.user_connected = kind == "user_connected"
            self.connection_reconciled = False
            self.emit(kind, **data)
            self.reconcile_requested = True
        elif kind == "feed_disconnected":
            self.emit("行情连接中断", **data)
            # 不盲目保留买入风险，退出仅撤本程序买单。
            for o in list(self.orders.values()):
                if o["side"] == "BUY" and o["status"] in ACTIVE:
                    self.rounds[o["slug"]]["entry_closed"] = True
                    await self.cancel(o, "行情断线")
        elif kind == "feed_connected":
            self.emit("行情订阅连接成功", **data)

    async def drain_user_messages(self):
        # 账户消息独立排队，避免大量盘口更新饿死成交及止盈处理。
        for _ in range(64):
            if self.user_queue.empty():
                break
            kind, data = self.user_queue.get_nowait()
            await self.on_message(kind, data)

    async def account_worker(self):
        while True:
            try:
                kind, data = await self.user_queue.get()
                await self.on_message(kind, data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connection_reconciled = False
                self.limited('账户推送处理异常', 'user', **error_fields(exc))

    async def drain_messages(self):
        started = time.monotonic()
        for _ in range(200):
            if self.queue.empty() or time.monotonic() - started > 0.05:
                break
            kind, data = self.queue.get_nowait()
            await self.on_message(kind, data)

    async def housekeeping(self):
        while not self.stopping:
            if time.time() - self.api.clock_checked > 60:
                try:
                    async with asyncio.timeout(15):
                        await self.api.sync_clock()
                except Exception as exc:
                    self.limited("时钟同步失败", "clock", **error_fields(exc))
            if self.mode == "live" and self.api.now() - self.last_geo > 300:
                self.geo_allowed = False
                try:
                    async with asyncio.timeout(15):
                        geo = await self.api.geoblock()
                    self.geo_allowed = geo.get("blocked") is False
                    self.last_geo = self.api.now()
                except Exception as exc:
                    self.limited("地区检查失败暂停新增买单", "geo", **error_fields(exc))
            await asyncio.sleep(10)

    async def archive_worker(self):
        while not self.stopping:
            try:
                if isinstance(self.broker, LiveBroker):
                    await self.accept_fills(await self.broker.reconcile(archived=True))
                for o in list(self.orders.values()):
                    if self.api.now() >= self.rounds[o["slug"]]["market"]["end"] and o["status"] in ACTIVE:
                        await self.cancel(o, "历史轮次已结束")
                await self.settle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.limited("历史轮次核实失败", "archive", **error_fields(exc))
            self.save()
            await asyncio.sleep(30)

    def resume_waiting_rounds(self):
        enabled = {r.id for r in self.config.markets if r.enabled}
        for slug, r in self.rounds.items():
            if (r["rule"]["id"] in enabled and self.api.now() < r["market"]["start"]
                    and r["skip_reason"] == "配置关闭或安全停机" and not r["entry_attempted"]
                    and not r.get("settlement") and not any(o["slug"] == slug for o in self.orders.values())
                    and r["status"] != "FINAL"):
                r.update(entry_closed=False, status="WAITING", skip_reason="")
                self.db.put_round(r)
                self.emit("恢复待预挂轮次", slug=slug)

    async def discover_rule_loop(self, rule):
        while not self.stopping:
            try:
                async with asyncio.timeout(20):
                    market = await self.api.discover(rule)
                if market:
                    previous = self.rounds.get(market.slug)
                    self.limited("市场发现检查", rule.id, slug=market.slug,
                                 start=utc_iso(market.start), existing=previous is not None,
                                 entry_closed=previous.get("entry_closed") if previous else False,
                                 skip_reason=previous.get("skip_reason", "") if previous else "")
                    await self.queue.put(("discovered", (rule, market)))
                else:
                    self.limited("未找到下一轮市场", rule.id)
            except Exception as exc:
                self.limited("市场发现失败", rule.id, **error_fields(exc))
            await asyncio.sleep(self.config.settings["discovery_interval_seconds"])

    async def discover_loop(self):
        tasks = {}
        try:
            while not self.stopping:
                rules = {r.id: r for r in self.config.markets if r.enabled}
                for ident in list(tasks):
                    rule, task = tasks[ident]
                    if rules.get(ident) != rule:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        del tasks[ident]
                for ident, rule in rules.items():
                    if ident not in tasks:
                        tasks[ident] = (rule, asyncio.create_task(self.discover_rule_loop(rule)))
                await asyncio.sleep(1)
        finally:
            for _, task in tasks.values():
                task.cancel()
            await asyncio.gather(*(task for _, task in tasks.values()), return_exceptions=True)

    async def run(self, config_path, run_seconds=None):
        from .reporting import report
        discovery = asyncio.create_task(self.discover_loop())
        housekeeping = asyncio.create_task(self.housekeeping())
        account_worker = asyncio.create_task(self.account_worker())
        archive_worker = asyncio.create_task(self.archive_worker()) if self.mode == "live" else None
        config_mtime = Path(config_path).stat().st_mtime_ns
        try:
            await self.reconcile()
            self.resume_waiting_rounds()
            while not self.stopping:
                if run_seconds and time.time() - self.started_at >= run_seconds:
                    self.stop_reason = "达到限定运行时间"
                    self.stopping = True
                    break
                try:
                    mtime = Path(config_path).stat().st_mtime_ns
                    if mtime != config_mtime:
                        try:
                            updated = Config.load(config_path)
                            if self.mode == "paper" and updated.settings["paper_initial_balance"] != self.config.settings["paper_initial_balance"]:
                                raise ValueError("运行中不能修改模拟初始资金")
                            self.config = updated
                            self.emit("配置已热更新", enabled=[r.id for r in updated.markets if r.enabled])
                        except Exception as exc:
                            self.emit("配置无效保留原配置", "ERROR", error_type=type(exc).__name__)
                        config_mtime = mtime
                    await self.drain_messages()
                    tokens = {t for r in self.rounds.values() if r["status"] != "FINAL" and self.api.now() < r["market"]["end"] for t in r["market"]["tokens"].values()}
                    self.feed.specs = {t: {"tick_size": r["market"]["tick"], "min_order_size": r["market"]["min_size"]}
                                       for r in self.rounds.values() for t in r["market"]["tokens"].values() if t in tokens}
                    await self.feed.set_tokens(tokens)
                    if self.reconcile_requested or self.api.now() - self.last_reconcile >= self.config.settings["reconcile_interval_seconds"]:
                        self.reconcile_requested = False
                        await self.reconcile()
                    await self.manage()
                    if self.mode != "live" and self.api.now() - self.last_settlement >= 30:
                        try:
                            await self.settle()
                        except Exception as exc:
                            self.limited("结算查询失败", "settlement", error_type=type(exc).__name__)
                        self.last_settlement = self.api.now()
                    if time.time() - self.last_status >= self.config.settings["status_interval_seconds"]:
                        stats = report(self)
                        self.emit("运行统计", **stats)
                        self.last_status = time.time()
                except Exception as exc:
                    self.connection_reconciled = False
                    self.emit("运行异常暂停新增风险", "ERROR", error_type=type(exc).__name__)
                await asyncio.sleep(0.2)
        except BaseException as exc:
            self.stop_reason = f"运行中断：{type(exc).__name__}"
            raise
        finally:
            self.stopping = True
            discovery.cancel()
            housekeeping.cancel()
            account_worker.cancel()
            if archive_worker:
                archive_worker.cancel()
                await asyncio.gather(archive_worker, return_exceptions=True)
            await asyncio.gather(discovery, housekeeping, account_worker, return_exceptions=True)
            self.emit("安全停机开始", reason=self.stop_reason)
            await self.reconcile()
            for o in list(self.orders.values()):
                if o["side"] == "BUY" and o["status"] in ACTIVE:
                    await self.cancel(o, "安全停机")
            # 留出有限的确认时间；停机期间仍接收用户成交/取消回报。
            stop_deadline = time.monotonic() + 15
            while any(o["side"] == "BUY" and o["status"] in ACTIVE for o in self.orders.values()) and time.monotonic() < stop_deadline:
                await asyncio.sleep(0.5)
                await self.drain_user_messages()
                await self.drain_messages()
                await self.reconcile()
                for o in list(self.orders.values()):
                    if o["side"] == "BUY" and o["status"] in ACTIVE:
                        await self.cancel(o, "停机确认剩余买单")
            await self.reconcile()
            for r in self.rounds.values():
                await self.protect(r)
            self.save()
            report(self)
            self.emit("安全停机完成", unknown_orders=sum(o["status"] in ACTIVE and o["side"] == "BUY" for o in self.orders.values()))
