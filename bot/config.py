from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

PERIODS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}


def D(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("金额和数量必须为有限数值")
    return result


@dataclass(frozen=True)
class Rule:
    id: str
    enabled: bool
    symbol: str
    timeframe: str
    entry_price: str
    shares: str
    take_profit_price: str
    entry_window_seconds: int
    preopen_seconds: int = 86400
    take_profit_enabled: bool = True

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("enabled 必须是 true 或 false")
        if type(self.take_profit_enabled) is not bool:
            raise ValueError("take_profit_enabled 必须是 true 或 false")
        if self.symbol not in {"BTC", "ETH", "SOL"} or self.timeframe not in PERIODS:
            raise ValueError("仅支持 BTC/ETH/SOL 和 5m/15m/1h/4h")
        if not 0 < D(self.entry_price) < D(self.take_profit_price) < 1:
            raise ValueError("必须满足 0 < 买价 < 固定止盈价 < 1")
        if D(self.shares) <= 0 or D(self.shares) % D("0.01"):
            raise ValueError("份额必须大于 0，最多两位小数")
        if type(self.entry_window_seconds) is not int or not 0 < self.entry_window_seconds < PERIODS[self.timeframe]:
            raise ValueError("挂单时间须为正整数且短于市场周期")
        if type(self.preopen_seconds) is not int or self.preopen_seconds < 0:
            raise ValueError("提前挂单时间须为非负整数")

    @property
    def series_slug(self):
        suffix = {"1h": "hourly"}.get(self.timeframe, self.timeframe)
        return f"{self.symbol.lower()}-up-or-down-{suffix}"


@dataclass
class Config:
    settings: dict
    markets: list[Rule]

    @classmethod
    def load(cls, path="markets.json"):
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        rules = [Rule(**r) for r in raw["markets"]]
        if len({r.id for r in rules}) != len(rules):
            raise ValueError("市场配置 id 重复")
        pairs = [(r.symbol, r.timeframe) for r in rules]
        if len(set(pairs)) != len(pairs):
            raise ValueError("同一币种周期不能重复配置")
        s = raw["settings"]
        for name in ("max_open_exposure", "max_daily_loss", "paper_initial_balance", "paper_queue_multiplier"):
            if D(s[name]) <= 0:
                raise ValueError(f"{name} 必须大于 0")
        for name in ("discovery_interval_seconds", "reconcile_interval_seconds", "status_interval_seconds", "max_clock_skew_seconds", "book_max_age_seconds"):
            if type(s[name]) is not int or s[name] <= 0:
                raise ValueError(f"{name} 必须为正整数")
        for name in ("paper_latency_ms", "cancel_before_deadline_seconds"):
            if type(s[name]) is not int or s[name] < 0:
                raise ValueError(f"{name} 必须为非负整数")
        if any(s["cancel_before_deadline_seconds"] >= r.entry_window_seconds for r in rules):
            raise ValueError("撤单提前量须短于开仓窗口")
        return cls(s, rules)


def live_enabled():
    dry = os.getenv("DRY_RUN", "true").lower()
    live = os.getenv("ENABLE_LIVE_TRADING", "false").lower()
    if dry not in {"true", "false"} or live not in {"true", "false"}:
        raise ValueError("DRY_RUN / ENABLE_LIVE_TRADING 必须为 true 或 false")
    if dry == "false":
        if live != "true" or os.getenv("LIVE_TRADING_ACK") != "I_UNDERSTAND_REAL_ORDERS":
            raise ValueError("实盘必须同时设置 ENABLE_LIVE_TRADING=true 和 LIVE_TRADING_ACK=I_UNDERSTAND_REAL_ORDERS")
        if not os.getenv("POLYMARKET_PRIVATE_KEY") or not os.getenv("POLYMARKET_WALLET_ADDRESS"):
            raise ValueError("实盘缺少私钥或账户钱包地址")
        return True
    if live == "true":
        raise ValueError("DRY_RUN 与 ENABLE_LIVE_TRADING 冲突")
    return False
