import json
from dataclasses import asdict
from types import SimpleNamespace as NS
import pytest
from bot.config import Config
from bot.lock import ProcessLock
from bot.reporting import report
from bot.models import Fill
from main import market_command, parser


def test_market_commands_validate_and_preserve_other_rules(tmp_path):
    path = tmp_path / "markets.json"
    raw = json.loads(open("markets.json", encoding="utf-8").read())
    path.write_text(json.dumps(raw), encoding="utf-8")
    market_command(NS(config=str(path), action="disable", id="btc-4h"))
    assert not Config.load(path).markets[0].enabled
    market_command(NS(config=str(path), action="enable", id="sol-15m"))
    market_command(NS(config=str(path), action="set", id="sol-15m", field="take_profit_price", value="0.25"))
    r = next(r for r in Config.load(path).markets if r.id == "sol-15m")
    assert r.enabled and r.take_profit_price == "0.25" and r.entry_window_seconds == 120
    before = path.read_text()
    with pytest.raises(ValueError):
        market_command(NS(config=str(path), action="set", id="sol-15m", field="take_profit_price", value="0.05"))
    assert path.read_text() == before


def test_round_keeps_original_strategy_parameters(engine):
    from dataclasses import replace
    updated = replace(engine.config.markets[0], take_profit_price="0.30", take_profit_enabled=False)
    engine.config.markets[0] = updated
    engine.add_round(replace(engine.market("test-round"), slug="next-round"), updated)
    old_rule = engine.rounds["test-round"]["rule"]
    assert old_rule["take_profit_price"] == "0.20" and old_rule["take_profit_enabled"]
    assert not engine.rounds["next-round"]["rule"]["take_profit_enabled"]


def test_take_profit_switch_accepts_boolean_and_leaves_market_enabled(tmp_path, capsys):
    path = tmp_path / "markets.json"
    raw = json.loads(open("markets.json", encoding="utf-8").read())
    next(r for r in raw["markets"] if r["id"] == "eth-5m")["enabled"] = True
    path.write_text(json.dumps(raw), encoding="utf-8")
    cli = parser()
    base = ["--config", str(path), "market"]
    change = base + ["set", "eth-5m", "take_profit_enabled"]

    market_command(cli.parse_args(change + ["false"]))
    rule = next(r for r in Config.load(path).markets if r.id == "eth-5m")
    assert not rule.take_profit_enabled and rule.enabled
    market_command(cli.parse_args(base + ["list"]))
    assert "止盈=关闭（持有到结算）" in capsys.readouterr().out

    with pytest.raises(ValueError, match="take_profit_enabled"):
        market_command(cli.parse_args(change + ["0"]))
    market_command(cli.parse_args(change + ["true"]))
    assert next(r for r in Config.load(path).markets if r.id == "eth-5m").take_profit_enabled


def test_single_instance_lock(tmp_path):
    path = tmp_path / "db.sqlite"
    with ProcessLock(path):
        with pytest.raises(RuntimeError):
            with ProcessLock(path):
                pass


def test_report_outputs_separate_mode_and_no_fabricated_fills(engine, tmp_path):
    engine.report_directory = tmp_path / "report"
    stats = report(engine)
    assert stats["accounting"] == "estimated" and stats["buy_total"] == "0"
    assert stats["win_rate"] is None
    assert (tmp_path / "report/summary.json").exists()
    assert (tmp_path / "report/rounds.csv").exists()
    assert len((tmp_path / "report/trades.csv").read_text(encoding="utf-8-sig").splitlines()) == 1


async def test_simulated_cancel_has_latency_and_can_race(engine):
    engine.config.settings["paper_latency_ms"] = 500
    await engine.manage()
    o = next(iter(engine.orders.values()))
    await engine.cancel(o, "test")
    assert o["status"] == "CANCEL_PENDING"
    engine.api.value += 1
    await engine.reconcile()
    assert o["status"] == "CANCELED"


async def test_expiry_cancels_outstanding_take_profit(engine):
    r = engine.rounds["test-round"]
    buy = await engine.submit(r, "up", "BUY", "0.10", "10", "ENTRY")
    await engine.accept_fills([asdict(Fill("buy", buy["local_id"], "0.10", "10", "0", "actual", engine.api.now(), "MAKER"))])
    await engine.manage()
    sell = next(o for o in engine.orders.values() if o["side"] == "SELL")
    engine.api.value = r["market"]["end"] + 1
    await engine.manage()
    assert sell["status"] == "CANCELED"


async def test_disconnect_cancels_entries_without_selling_inventory(engine):
    await engine.manage()
    await engine.on_message("feed_disconnected", {"error_type":"ConnectionError"})
    assert all(o["status"] == "CANCELED" for o in engine.orders.values())
    assert not any(o["side"] == "SELL" for o in engine.orders.values())


async def test_failed_reconcile_obeys_interval(engine):
    async def fail():
        raise RuntimeError("private response must not be logged")
    engine.broker.reconcile = fail
    await engine.reconcile()
    assert not engine.connection_reconciled
    assert engine.last_reconcile == engine.api.now()


async def test_failed_cancel_backs_off_without_losing_pending_state(engine, monkeypatch):
    await engine.manage()
    order = next(iter(engine.orders.values()))
    clock = [100.0]
    monkeypatch.setattr("bot.engine.time.monotonic", lambda: clock[0])
    calls = []
    async def fail(o):
        calls.append(o["local_id"])
        raise RuntimeError("private response must not be logged")
    engine.broker.cancel = fail
    await engine.cancel(order, "test")
    await engine.cancel(order, "test")
    assert len(calls) == 1 and order["status"] == "CANCEL_PENDING"
    clock[0] += 2
    await engine.cancel(order, "test")
    assert len(calls) == 2
    clock[0] += 2
    await engine.cancel(order, "test")
    assert len(calls) == 2


def test_error_details_exclude_response_values():
    from pydantic import BaseModel, ValidationError
    from bot.logger import error_fields
    class Payload(BaseModel):
        balance: int
    try:
        try:
            Payload(balance="private-secret")
        except ValidationError as cause:
            raise RuntimeError("private-secret") from cause
    except RuntimeError as exc:
        details = error_fields(exc)
    assert details["validation_errors"] == [{"field": "balance", "type": "int_parsing"}]
    assert "private-secret" not in json.dumps(details)


async def test_reconcile_command_backs_up_and_only_reads_account(engine, tmp_path, monkeypatch):
    import logging
    import time
    import main
    from bot.database import Database
    path = tmp_path / "live.db"
    db = Database(path)
    with db.transaction(): db.set_meta("preserved", "history")
    db.close()
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setattr(main, "live_enabled", lambda: True)
    monkeypatch.setattr(main, "setup", lambda mode: logging.getLogger("test"))
    calls = []
    async def sync(): calls.append("clock")
    async def close(): calls.append("close")
    async def reconcile():
        calls.append("read_account")
        return []
    async def create(e): return NS(reconcile=reconcile, close=close)
    monkeypatch.setattr(main, "PublicAPI", lambda: NS(now=time.time, sync_clock=sync, close=close))
    monkeypatch.setattr(main.LiveBroker, "create", create)
    def report(e):
        assert e.stopping is True
        calls.append("report")
    monkeypatch.setattr("bot.reporting.report", report)
    assert await main.reconcile_account(engine.config) == 0
    backups = list(tmp_path.glob("live-before-reconcile-*.db"))
    assert len(backups) == 1
    restored = Database(backups[0])
    assert restored.meta("preserved") == "history"
    restored.close()
    assert calls == ["clock", "read_account", "report", "close", "close"]


async def test_timed_shutdown_records_reason(engine, tmp_path):
    engine.started_at -= 10
    engine.report_directory = tmp_path / 'report'
    async def discovery():
        return
    engine.discover_loop = discovery
    await engine.run('markets.json', run_seconds=1)
    row = engine.db.conn.execute("SELECT data FROM bot_events WHERE event='安全停机开始'").fetchone()
    assert json.loads(row[0])['reason'] == '达到限定运行时间'


async def test_cancelled_shutdown_records_reason(engine, tmp_path):
    import asyncio
    engine.report_directory = tmp_path / 'report'
    async def discovery():
        return
    engine.discover_loop = discovery
    original = engine.reconcile
    calls = 0
    async def cancel_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError()
        await original()
    engine.reconcile = cancel_once
    with pytest.raises(asyncio.CancelledError):
        await engine.run('markets.json')
    row = engine.db.conn.execute("SELECT data FROM bot_events WHERE event='安全停机开始'").fetchone()
    assert json.loads(row[0])['reason'] == '运行中断：CancelledError'


async def test_account_queue_processed_first_and_market_backlog_is_bounded(engine):
    seen = []
    async def handle(kind, data):
        seen.append(kind)
        if kind == 'market':
            engine.queue.put_nowait(('market', {}))
    engine.on_message = handle
    engine.user_queue.put_nowait(('account', {}))
    engine.queue.put_nowait(('market', {}))
    await engine.drain_user_messages()
    await engine.drain_messages()
    assert seen[0] == 'account'
    assert len(seen) <= 201
    assert not engine.queue.empty()


async def test_slow_clock_task_does_not_block_account_queue(engine):
    import asyncio
    entered = asyncio.Event()
    async def slow_clock():
        entered.set()
        await asyncio.Event().wait()
    engine.api.clock_checked = 0
    engine.api.sync_clock = slow_clock
    task = asyncio.create_task(engine.housekeeping())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        engine.user_queue.put_nowait(('user_connected', {}))
        await asyncio.wait_for(engine.drain_user_messages(), 1)
        assert engine.reconcile_requested
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
