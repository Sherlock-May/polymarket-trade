import json
from dataclasses import replace
import pytest
from bot.config import Config, Rule, D, live_enabled
from bot.models import Market, Book
from bot.pnl_calculator import estimate_fee, position
from bot.market_discovery import PublicAPI
from bot.websocket_client import MarketFeed


async def test_clock_sync_uses_lowest_latency_sample(monkeypatch):
    import bot.market_discovery as discovery
    clock = iter([100, 104, 105, 105.2, 106, 107, 107.1])
    monkeypatch.setattr(discovery.time, "time", lambda: next(clock))
    api = PublicAPI()
    samples = iter([102, 105, 106])
    async def get(url): return next(samples)
    api.get = get
    try:
        await api.sync_clock()
        assert api.clock_uncertainty == pytest.approx(1.1)
        assert api.offset == pytest.approx(-0.1)
    finally:
        await api.close()


async def test_market_query_finds_closed_round():
    api = PublicAPI()
    calls = []
    async def get(url, **params):
        calls.append(params)
        return [{"slug": "old", "closed": True}] if params.get("closed") == "true" else []
    api.get = get
    try:
        assert (await api.market("old"))["closed"] is True
        assert calls == [{"slug": "old"}, {"slug": "old", "closed": "true"}]
    finally:
        await api.close()


def test_config_honors_explicit_market_selection(tmp_path):
    # 用户可以热更新根配置；测试默认预设不依赖当前启用了哪个市场。
    raw = json.loads(open("markets.json", encoding="utf-8").read())
    for rule in raw["markets"]:
        rule["enabled"] = rule["id"] == "btc-4h"
    path = tmp_path / "markets.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    c = Config.load(path)
    assert [r.id for r in c.markets if r.enabled] == ["btc-4h"]
    assert c.markets[0].entry_window_seconds == 3600


@pytest.mark.parametrize("field,value", [("entry_price", "NaN"), ("shares", "0"), ("shares", "10.001"), ("take_profit_price", "0.05"), ("entry_window_seconds", 14400), ("enabled", "false")])
def test_invalid_rules_rejected(field, value):
    with pytest.raises(ValueError):
        replace(Config.load().markets[0], **{field: value})


def test_live_requires_all_switches(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    monkeypatch.delenv("LIVE_TRADING_ACK", raising=False)
    with pytest.raises(ValueError):
        live_enabled()


def test_private_key_never_enables_live(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "anything")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    assert live_enabled() is False


def sample():
    return {"slug": "btc-updown-4h-1794819600", "conditionId": "condition", "eventStartTime": "2026-11-16T09:00:00Z",
            "endDate": "2026-11-16T13:00:00Z", "outcomes": '["Down","Up"]', "clobTokenIds": '["down-id","up-id"]',
            "active": True, "closed": False, "acceptingOrders": True, "enableOrderBook": True,
            "orderPriceMinTickSize": "0.01", "orderMinSize": "5", "feeSchedule": {"rate":"0.07", "exponent":1, "takerOnly":True}}


def test_map_outcomes_by_labels_not_assumed_index():
    m = Market.parse(sample(), {}, Config.load().markets[0])
    assert m.tokens == {"DOWN": "down-id", "UP": "up-id"}
    assert m.end - m.start == 14400


def test_creation_startdate_is_not_round_start():
    m = sample()
    del m["eventStartTime"]
    m["startDate"] = "2026-11-15T09:00:00Z"
    with pytest.raises(ValueError):
        Market.parse(m, {}, Config.load().markets[0])


def test_reject_wrong_actual_duration():
    raw = sample()
    raw["endDate"] = "2026-11-16T10:00:00Z"
    with pytest.raises(ValueError):
        Market.parse(raw, {}, Config.load().markets[0])


@pytest.mark.parametrize("price,size", [("0.105","10"), ("0.10","4"), ("1","10")])
def test_tick_and_min_size(price, size):
    m = Market.parse(sample(), {}, Config.load().markets[0])
    with pytest.raises(ValueError):
        m.validate_order(price, size)


def test_fee_curve():
    m = Market.parse(sample(), {}, Config.load().markets[0])
    assert estimate_fee(m, "0.10", "10", "TAKER") == D("0.063")
    assert estimate_fee(m, "0.10", "10", "MAKER") == 0


def test_liquidation_uses_depth():
    b = Book.parse({"asset_id":"x", "timestamp":"1000", "bids":[{"price":"0.20","size":"2"},{"price":"0.10","size":"3"}], "asks":[]}, 1)
    assert b.liquidation(10) == (D("0.7"), D(5))


async def test_discovery_uses_series_actual_time_not_utc_floor(monkeypatch):
    a = PublicAPI()
    a.now = lambda: 1794816600  # 08:10 UTC；下一轮真实开盘 09:00 UTC
    raw = sample()
    async def get(url, **params):
        if url.endswith("/series"):
            return [{"id":"series-id", "slug":"btc-up-or-down-4h"}]
        assert params["series_id"] == "series-id"
        return [{"startTime":"2026-11-16T09:00:00Z", "markets":[raw]}]
    a.get = get
    try:
        m = await a.discover(Config.load().markets[0])
        assert m.start == 1794819600
    finally:
        await a.close()


def test_book_snapshot_and_price_change():
    f = MarketFeed(None, None)
    f.apply({"type":"book", "timestamp":1, "received_at":2, "payload":{"asset_id":"up", "bids":[{"price":"0.10","size":"5"}], "asks":[]}})
    f.apply({"type":"price_change", "timestamp":2, "received_at":3, "payload":{"price_changes":[{"asset_id":"up", "side":"BUY", "price":"0.10", "size":"0"}]}})
    assert not f.books["up"].bids
    f.apply({"type":"price_change", "timestamp":1, "received_at":4, "payload":{"price_changes":[{"asset_id":"up", "side":"BUY", "price":"0.10", "size":"100"}]}})
    assert not f.books["up"].bids


def test_sdk_public_contracts_exist():
    import inspect
    from polymarket import AsyncSecureClient
    for method in ("create_limit_order", "post_order", "cancel_order", "get_order", "get_balance_allowance", "list_account_trades", "redeem_positions"):
        assert hasattr(AsyncSecureClient, method)
    assert "expiration" in inspect.signature(AsyncSecureClient.create_limit_order).parameters
