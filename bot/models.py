from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal
from .config import D, PERIODS, Rule


def epoch(value):
    if not value:
        raise ValueError("缺少真实开盘/结束时间")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def utc_iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


@dataclass
class Market:
    slug: str
    condition_id: str
    start: float
    end: float
    tokens: dict[str, str]
    tick: str
    min_size: str
    accepting: bool
    fee_rate: str
    fee_exponent: str
    fee_taker_only: bool
    fee_known: bool
    version: str = "v1"

    @classmethod
    def parse(cls, m, event, rule: Rule):
        import json
        outcomes = m.get("outcomes", [])
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
        version = m.get("version", "v1")
        assets = m.get("positionIds" if version == "v2" else "clobTokenIds", [])
        assets = json.loads(assets) if isinstance(assets, str) else assets
        if len(outcomes) != 2 or len(assets) != 2 or {str(o).upper() for o in outcomes} != {"UP", "DOWN"}:
            raise ValueError("目标必须有经过核实的 UP/DOWN 和两个交易资产 ID")
        if not m.get("conditionId") or len(set(assets)) != 2 or not all(assets):
            raise ValueError("condition_id 或资产 ID 无效")
        start = epoch(m.get("eventStartTime") or event.get("startTime"))
        end = epoch(m.get("endDate") or event.get("endDate"))
        if abs((end - start) - PERIODS[rule.timeframe]) > 1:
            raise ValueError("市场实际周期与配置不符")
        fee = m.get("feeSchedule") or {}
        known = m.get("feesEnabled") is False or ("rate" in fee and "exponent" in fee and "takerOnly" in fee)
        return cls(m["slug"], m["conditionId"], start, end,
                   {str(o).upper(): str(t) for o, t in zip(outcomes, assets)},
                   str(m["orderPriceMinTickSize"]), str(m["orderMinSize"]),
                   m.get("acceptingOrders") is True and m.get("closed") is False and m.get("enableOrderBook") is True,
                   str(fee.get("rate", "0")), str(fee.get("exponent", "1")),
                   fee.get("takerOnly", True), known, version)

    def validate_order(self, price, size):
        if not self.accepting:
            raise ValueError("市场不接受订单")
        if D(size) < D(self.min_size):
            raise ValueError(f"数量不足最小份额 {self.min_size}")
        if not 0 < D(price) < 1 or D(price) % D(self.tick):
            raise ValueError(f"价格不符合步长 {self.tick}")
        if D(size) % D("0.01"):
            raise ValueError("份额精度超过两位小数")


@dataclass
class Book:
    asset_id: str
    bids: dict[Decimal, Decimal]
    asks: dict[Decimal, Decimal]
    received_at: float
    timestamp: float
    tick: str = "0.01"
    min_size: str = "5"
    synced: bool = True

    @classmethod
    def parse(cls, raw, received_at):
        return cls(str(raw["asset_id"]),
                   {D(x["price"]): D(x["size"]) for x in raw.get("bids", []) if D(x["size"]) > 0},
                   {D(x["price"]): D(x["size"]) for x in raw.get("asks", []) if D(x["size"]) > 0},
                   received_at, float(raw.get("timestamp", 0)) / 1000,
                   str(raw.get("tick_size") or "0.01"), str(raw.get("min_order_size") or "5"))

    def liquidation(self, shares):
        remaining, proceeds = D(shares), D(0)
        for price, size in sorted(self.bids.items(), reverse=True):
            qty = min(size, remaining)
            proceeds += price * qty
            remaining -= qty
            if remaining <= 0:
                break
        return proceeds, remaining


@dataclass
class Fill:
    id: str
    order_id: str
    price: str
    shares: str
    fee: str
    fee_kind: str
    timestamp: float
    liquidity: str
    status: str = "CONFIRMED"
    tx_hash: str = ""
