import logging
import time
from dataclasses import replace
from types import SimpleNamespace
import pytest
from bot.config import Config, D
from bot.database import Database
from bot.engine import Engine
from bot.models import Market, Book
from bot.polymarket_client import PaperBroker


@pytest.fixture
def engine(tmp_path):
    c = Config.load()
    c.settings["paper_latency_ms"] = 0
    c.settings["paper_queue_multiplier"] = "1"
    c.settings["cancel_before_deadline_seconds"] = 0
    now = time.time()
    api = SimpleNamespace(value=now, offset=0, clock_checked=now, clock_uncertainty=0)
    api.now = lambda: api.value
    feed = SimpleNamespace(books={}, connected=True)
    db = Database(tmp_path / "test.db")
    e = Engine(db, c, api, feed, logging.getLogger("test"))
    e.broker = PaperBroker(e)
    rule = replace(c.markets[0], enabled=True, timeframe="15m", entry_window_seconds=120, take_profit_enabled=True)
    e.config.markets = [rule]
    m = Market("test-round", "condition", now + 60, now + 960, {"UP": "up", "DOWN": "down"}, "0.01", "5", True, "0.07", "1", True, True)
    e.add_round(m, rule)
    for token in m.tokens.values():
        feed.books[token] = Book(token, {D("0.09"): D(20)}, {D("0.50"): D(20)}, time.time(), now)
    yield e
    db.close()
